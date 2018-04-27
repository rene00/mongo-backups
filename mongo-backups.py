#!/usr/bin/env python3

__VERSION__ = '0.1'

from pymongo import MongoClient
from datetime import datetime as dt
import argparse
import boto3
import fnmatch
import os
import re
import requests
import subprocess
import pprint   # noqa
import sys
import time
import string
import tempfile
import lvm
import math
import tzlocal

TZ = tzlocal.get_localzone()


def parse_args():
    """ Do all command line parsing and return the results as an argparse
        Namespace. """

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--mongo-name', dest='mongo_name', required=True,
        help='The name of the mongo cluster.'
    )
    parser.add_argument(
        '--aws-region', dest='aws_region', required=True,
        help='The name of the AWS region the mongo cluster exists within.'
    )
    parser.add_argument(
        '--wait-time', dest='wait_time', type=int,
        required=False, default=60,
        help=('The time in seconds to wait for some operations to complete.')
    )
    parser.add_argument(
        '--action', dest='action', nargs='?',
        choices=('dump', 'backup', 'latest_block_device'),
        default='dump'
    )
    parser.add_argument(
        '--vg-name', dest='vg_name',
        required=False, default='vgmongo',
        help=('The LVM volume group name (eg; vgmongo).')
    )
    parser.add_argument(
        '--lv-name', dest='lv_name',
        required=False, default='lvmongo',
        help=('The LVM logical volume name (eg; lvmongo).')
    )
    parser.add_argument(
        '--mongo-lock', dest='mongo_lock', action='store_true',
        default=False,
        help=('Lock Mongo before performing snapshot.')
    )
    parser.add_argument(
        '--seed-from-last-snapshot', dest='seed_from_last_snapshot',
        action='store_true', default=False,
        help=('Seed the volume from the last snapshot.')
    )
    return parser.parse_args()


class MongoBackups:
    def __init__(self, mongo_name, aws_region, vg_name, lv_name):
        self.mongo_name = mongo_name
        self.aws_region = aws_region
        self.vg_name = vg_name
        self.lv_name = lv_name

        # A dict which will hold stats that are added to the snapshot as tags
        # and will be used for reporting.
        self.stats = {}

    @property
    def session(self):
        """ A session to AWS. """

        return boto3.session.Session()

    @property
    def client(self):
        """ A client connection to EC2. """

        return self.session.client('ec2', self.aws_region)

    @property
    def ec2(self):
        """ An EC2 session resource connection. """

        return self.session.resource('ec2', self.aws_region)

    @property
    def instance(self):
        """ A instance connection to EC2. """

        return self.ec2.Instance(self.instance_id)

    @property
    def instance_id(self):
        """Return the instance id of the instance running this script."""

        response = requests.get(
            'http://169.254.169.254/latest/meta-data/instance-id'
        )
        return response.text

    @property
    def volume_filter(self):
        """ Return a list of dicts which can be supplied as a filter to
            boto3's describe_volumes(). """

        return [
            {'Name': 'tag:MongoName', 'Values': [self.mongo_name]},
            {'Name': 'tag:MongoLiveVolume', 'Values': ['True']},
        ]

    def get_latest_block_device(self):
        """Return the latest block device to be attached by walking /dev."""

        includes = ['xvd*']
        includes = r'|'.join([fnmatch.translate(x) for x in includes])

        latest_block_device = None

        for root, dirs, files in os.walk('/dev/'):
            block_devices = [f for f in files if re.match(includes, f)]
            if block_devices:
                block_devices.sort()
                latest_block_device = block_devices[-1]

        return latest_block_device

    def get_next_free_block_device(self):
        """Return the next free block device by walking /dev.  """

        latest_block_device = self.get_latest_block_device()

        # Create a list of all potential block devices.
        all_block_devices = []
        for letter in list(string.ascii_lowercase):
            all_block_devices.append('xvd{0}'.format(letter))

        # Grab the index for the latest known block device.
        _index = all_block_devices.index(latest_block_device)

        # Grab the next free block device from the list.
        next_free_block_device = all_block_devices[_index + 1]

        return next_free_block_device

    def ebs_create_volume(self, size, volume_type, encrypted=True,
                          availability_zone=None, snapshot_id=None):
        """ Create an EBS volume."""

        if not availability_zone:
            availability_zone = self.instance.placement['AvailabilityZone']

        kwargs = {
            'AvailabilityZone': availability_zone,
            'VolumeType': volume_type,
            'Encrypted': encrypted
        }

        if snapshot_id:
            # wait till snapshot has completed first.
            print(
                "DEBUG: checking that snapshot is complete [{0}].".
                format(snapshot_id)
            )
            waiter = self.client.get_waiter('snapshot_completed')
            waiter.wait(SnapshotIds=[snapshot_id])
            kwargs['SnapshotId'] = snapshot_id
        else:
            kwargs['Size'] = size

        return self.client.create_volume(**kwargs)

    def ebs_create_snapshot(self, volume_id):
        """ Perform an EBS snapshot on volume_id. """

        # Set Name and Description tags to something like
        # "MongoBackups-customerA-i-00ab0281eff3b2a63".
        name = "MongoBackups-{0}-{1}".format(
            self.mongo_name, self.instance_id
        )
        description = name

        self.stats['date_finished'] = dt.now().isoformat()

        resp = self.client.create_snapshot(
            Description=description, VolumeId=volume_id,
            TagSpecifications=[
                {
                    'ResourceType': 'snapshot',
                    'Tags': [
                        {
                            'Key': 'InstanceId',
                            'Value': self.instance_id,
                        },
                        {
                            'Key': 'Name',
                            'Value': name,
                        },
                        {
                            'Key': 'Description',
                            'Value': description,
                        },
                        {
                            'Key': 'MongoName',
                            'Value': self.mongo_name,
                        },
                        {
                            'Key': 'MongoBackups',
                            'Value': 'True',
                        },
                        {
                            'Key': 'DateStarted',
                            'Value': self.stats['date_started'],
                        },
                        {
                            'Key': 'DateFinished',
                            'Value': self.stats['date_finished'],
                        },
                        {
                            'Key': 'MongoBackupsVersion',
                            'Value': __VERSION__,
                        }
                    ],
                }
            ]
        )
        return resp

    def ebs_detach_volume(self, volume_id, device):
        """ Detach an EBS volume. """

        return self.client.detach_volume(
            Device=device,
            Force=True,
            InstanceId=self.instance_id,
            VolumeId=volume_id
        )

    def ebs_delete_volume(self, volume_id):
        """ Delete an EBS volume. """

        return self.client.delete_volume(VolumeId=volume_id)

    @property
    def physical_block_devices(self):
        """ Return a list of physical block devices within the VG. """

        vg = lvm.vgOpen(self.vg_name, 'r')
        physical_block_devices = []
        for pv in vg.listPVs():
            physical_block_devices.append(pv.getName())
        return physical_block_devices

    @property
    def logical_volume(self):
        """ Return some information about the logical volume. """

        vg = lvm.vgOpen(self.vg_name, 'r')
        data = {'lvsize': 0}
        for lv in vg.listLVs():
            # getSize() is in Bytes.
            data['lvsize'] = (
                data['lvsize'] + (lv.getSize() / (1024*1024*1024))
            )
        # round up size to produce Gb.
        data['lvsize'] = math.ceil(data['lvsize'])

        return data

    @property
    def last_snapshot(self):
        """ Return a dict which represents the last snapshot. """

        _filter = [
            {'Name': 'tag:MongoName', 'Values': [self.mongo_name]},
            {'Name': 'tag:MongoBackups', 'Values': ['True']},
        ]

        snapshots = self.client.describe_snapshots(Filters=_filter)

        last_snapshot = {
            'date': TZ.localize(dt(1970, 1, 1)),
            'snapshot_id': None
        }

        for snapshot in snapshots['Snapshots']:
            start_time = snapshot['StartTime']
            if start_time > last_snapshot['date']:
                last_snapshot['date'] = snapshot['StartTime']
                last_snapshot['snapshot_id'] = snapshot['SnapshotId']

        return last_snapshot


def main():
    args = parse_args()

    mongo_backups = MongoBackups(
        args.mongo_name, args.aws_region, args.vg_name, args.lv_name
    )

    mongo_backups.stats['date_started'] = dt.now().isoformat()

    _filter = mongo_backups.volume_filter
    volumes = mongo_backups.client.describe_volumes(Filters=_filter)

    if args.action == 'dump':
        print(mongo_backups.aws_region)
        print(mongo_backups.instance_id)
        pprint.pprint(_filter, indent=4)
        pprint.pprint(volumes['Volumes'], indent=4)
        print(mongo_backups.physical_block_devices)
        print(mongo_backups.logical_volume)
        print(mongo_backups.last_snapshot)
    elif args.action == 'latest_block_device':
        print(mongo_backups.get_latest_block_device())
    elif args.action == 'backup':
        for volume in volumes['Volumes']:

            attached_instance_id = volume['Attachments'][0]['InstanceId']
            attached_device = volume['Attachments'][0]['Device']

            # Confirm that the live volume we are checking belongs to this
            # instance and shares the same block device attachment. If we dont
            # do this, we could backup a mongo instance we dont want backing
            # up.
            if (attached_instance_id == mongo_backups.instance_id and
                    attached_device in mongo_backups.physical_block_devices):

                # Create new volume.
                size = mongo_backups.logical_volume['lvsize']
                volume_type = volume['VolumeType']

                if args.seed_from_last_snapshot:
                    print(
                        "DEBUG: creating a new volume from the last "
                        "snapshot [snapshot_id={0}, volume_type={1}]."
                        .format(
                            mongo_backups.last_snapshot['snapshot_id'],
                            volume_type
                        )
                    )
                    if not mongo_backups.last_snapshot['snapshot_id']:
                        print("DEBUG: no snapshots exist yet.")
                        sys.exit(2)
                    else:
                        new_volume = mongo_backups.ebs_create_volume(
                            size=None, volume_type=volume_type,
                            snapshot_id=(
                                mongo_backups.last_snapshot['snapshot_id']
                            )
                        )
                else:
                    print(
                        "DEBUG: creating a new volume "
                        "[size={0}GB, volume_type={1}]."
                        .format(size, volume_type)
                    )
                    new_volume = mongo_backups.ebs_create_volume(
                        size, volume_type
                    )

                # Wait for new volume to be available.
                print(
                    "DEBUG: waiting for new volume to become available [{0}]."
                    .format(new_volume['VolumeId'])
                )
                waiter = mongo_backups.client.get_waiter('volume_available')
                waiter.wait(VolumeIds=[new_volume['VolumeId']])
                print(
                    "DEBUG: volume available [{0}].".
                    format(new_volume['VolumeId'])
                )

                last_block_device = mongo_backups.get_latest_block_device()
                print(
                    "DEBUG: last block device attached [{0}].".
                    format(last_block_device)
                )

                # Attach newly created volume.
                attach_device = mongo_backups.get_next_free_block_device()
                print(
                    "DEBUG: next available block device found [{0}]"
                    .format(attach_device)
                )
                print(
                    "DEBUG: attaching volume [volume_id={0}, device={1}].".
                    format(new_volume['VolumeId'], attach_device)
                )
                mongo_backups.client.attach_volume(
                    Device=attach_device,
                    InstanceId=mongo_backups.instance_id,
                    VolumeId=new_volume['VolumeId']
                )

                # wait whilst the volume attaches itself and is registered
                # with the kernel
                count = 0
                while count < args.wait_time:
                    latest_block_device = \
                        mongo_backups.get_latest_block_device()
                    time.sleep(1)
                    print("DEBUG: waiting for new block device to attach.")
                    if last_block_device != latest_block_device:
                        print(
                            "DEBUG: new block device attached [{0}].".
                            format(latest_block_device)
                        )
                        break

                # Create a filesystem on the new block device.
                subprocess.call(
                    'mkfs.xfs /dev/{0}'.
                    format(latest_block_device),
                    shell=True
                )

                # Make a temporary mount points for the new volume and LVM
                # snapshot.
                temp_mount_point_new_volume = tempfile.mkdtemp(
                    prefix='/media/'
                )
                temp_mount_point_lvsnap = tempfile.mkdtemp(prefix='/media/')

                # Mount the new block device at temporary mount point.
                subprocess.call(
                    'mount /dev/{0} {1}'.
                    format(latest_block_device, temp_mount_point_new_volume),
                    shell=True
                )

                # Lock mongo.
                if args.mongo_lock:
                    print("DEBUG: locking mongo.")
                    conn = MongoClient('mongodb://127.0.0.1:27017')
                    conn.fsync(lock=True)

                # Create LVM snapshot.
                subprocess.call(
                    'lvcreate -L300M -s -n lvsnap '
                    '/dev/mapper/{0}-{1}'
                    .format(mongo_backups.vg_name, mongo_backups.lv_name),
                    shell=True
                )

                # Unlock mongo.
                if args.mongo_lock:
                    print("DEBUG: unlocking mongo.")
                    conn.unlock()

                # Mount LVM snapshot in read-only.
                subprocess.call(
                    'mount -o nouuid,ro /dev/{0}/lvsnap {1}'.
                    format(mongo_backups.vg_name, temp_mount_point_lvsnap),
                    shell=True
                )

                # Rsync LVM snapshot to new volume.
                subprocess.call(
                    'rsync -avh --delete -p {0}/* {1}/'.
                    format(
                        temp_mount_point_lvsnap,
                        temp_mount_point_new_volume
                    ),
                    shell=True
                )

                # Unmount the LVM snapshot
                subprocess.call(
                    'umount {0}'.format(temp_mount_point_lvsnap),
                    shell=True
                )

                # Unmount the new volume.
                subprocess.call(
                    'umount {0}'.format(temp_mount_point_new_volume),
                    shell=True
                )

                # Remove the LVM snapshot.
                subprocess.call(
                    'lvremove -y /dev/{0}/lvsnap'.
                    format(mongo_backups.vg_name),
                    shell=True
                )

                # Create a snapshot of the new volume which now has a copy
                # of the database.
                snapshot = mongo_backups.ebs_create_snapshot(
                    new_volume['VolumeId']
                )

                # Detach the new volume.
                print(
                    "DEBUG: detaching new volume {0} which contains the "
                    "database backup.".
                    format(new_volume['VolumeId'])
                )
                mongo_backups.ebs_detach_volume(
                    new_volume['VolumeId'], attach_device
                )
                waiter = mongo_backups.client.get_waiter('volume_available')
                waiter.wait(VolumeIds=[new_volume['VolumeId']])
                print(
                    "DEBUG: volume {0} detached.".
                    format(new_volume['VolumeId'])
                )

                print(
                    "DEBUG: deleting new volume {0}."
                    .format(new_volume['VolumeId'])
                )
                mongo_backups.ebs_delete_volume(new_volume['VolumeId'])

                print(
                    "DEBUG: backup complete on snapshot {0}"
                    .format(snapshot['SnapshotId'])
                )

                sys.exit(0)


if __name__ == '__main__':
    sys.exit(main())
