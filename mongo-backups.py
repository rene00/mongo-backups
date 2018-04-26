#!/usr/bin/env python3

from pymongo import MongoClient
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

# FIXME
try:
    import lvm  # noqa
except ImportError:
    pass


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
        '--physical-block-device', dest='physical_block_device',
        required=False, default='/dev/xvde',
        help=(
            'FIXME: the name of the physical block device that belongs to '
            'the volume group.'
        )
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
        help=('The LVM volume group name (eg; vgmongo)')
    )
    parser.add_argument(
        '--lv-name', dest='lv_name',
        required=False, default='lvmongo',
        help=('The LVM logical volume name (eg; lvmongo)')
    )
    parser.add_argument(
        '--mongo-lock', dest='mongo_lock', action='store_true',
        default=False,
        help=('Lock Mongo before performing snapshot')
    )
    return parser.parse_args()


class MongoBackups:
    def __init__(self, mongo_name, aws_region):
        self.mongo_name = mongo_name
        self.aws_region = aws_region

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
    def snapshot_filter(self):
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
                          availability_zone=None):
        """ Create an EBS volume."""

        if not availability_zone:
            availability_zone = self.instance.placement['AvailabilityZone']

        return self.client.create_volume(
            Size=size, AvailabilityZone=availability_zone,
            VolumeType=volume_type, Encrypted=True,
        )

    def ebs_create_snapshot(self, volume_id):
        """ Perform an EBS snapshot on volume_id. """

        # Set Name and Description tags to something like
        # "MongoBackups-customerA-i-00ab0281eff3b2a63".
        name = "MongoBackups-{0}-{1}".format(
            self.mongo_name, self.instance_id
        )
        description = name

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


def main():
    args = parse_args()

    mongo_backups = MongoBackups(args.mongo_name, args.aws_region)

    _filter = mongo_backups.snapshot_filter
    volumes = mongo_backups.client.describe_volumes(Filters=_filter)

    if args.action == 'dump':
        print(mongo_backups.aws_region)
        print(mongo_backups.instance_id)
        pprint.pprint(_filter, indent=4)
        pprint.pprint(volumes['Volumes'], indent=4)

    elif args.action == 'latest_block_device':
        print(mongo_backups.get_latest_block_device())
    elif args.action == 'backup':
        for volume in volumes['Volumes']:

            attached_instance_id = volume['Attachments'][0]['InstanceId']
            attached_device = volume['Attachments'][0]['Device']
            if (attached_instance_id == mongo_backups.instance_id and
                    attached_device == args.physical_block_device):

                # Create new volume based off existing live attached volume.
                print("DEBUG: creating a new volume.")
                size = volume['Size']
                volume_type = volume['VolumeType']
                new_volume = mongo_backups.ebs_create_volume(
                    size, volume_type
                )

                # Wait for new volume to be available.
                print(
                    "DEBUG: waiting for new volume {0} to become available."
                    .format(new_volume['VolumeId'])
                )
                waiter = mongo_backups.client.get_waiter('volume_available')
                waiter.wait(VolumeIds=[new_volume['VolumeId']])
                print(
                    "DEBUG: volume {0} available".
                    format(new_volume['VolumeId'])
                )

                last_block_device = mongo_backups.get_latest_block_device()
                print(
                    "DEBUG: last block device attached is {0}".
                    format(last_block_device)
                )

                # Attach newly created volume.
                attach_device = mongo_backups.get_next_free_block_device()
                print(
                    "DEBUG: next available block device is {0}"
                    .format(attach_device)
                )
                print(
                    "DEBUG: attaching {0} as {1}".
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
                            "DEBUG: new block device {0} attached.".
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
                    conn = MongoClient('mongodb://127.0.0.1:27017')
                    conn.fsync(lock=True)

                # Create LVM snapshot.
                subprocess.call(
                    'lvcreate -L300M -s -n lvsnap '
                    '/dev/mapper/{0}-{1}'
                    .format(args.vg_name, args.lv_name),
                    shell=True
                )

                # Mount LVM snapshot in read-only.
                subprocess.call(
                    'mount -o nouuid,ro /dev/{0}/lvsnap {1}'.
                    format(args.vg_name, temp_mount_point_lvsnap),
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

                # Unlock mongo.
                if args.mongo_lock:
                    conn.unlock()

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
                    'lvremove -y /dev/{0}/lvsnap'.format(args.vg_name),
                    shell=True
                )

                # Create a snapshot of the new volume which now has a copy
                # of the database.
                snapshot = mongo_backups.ebs_snapshot(new_volume['VolumeId'])

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
                    .format(snapshot)
                )


if __name__ == '__main__':
    sys.exit(main())
