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
import sys
import time
import string
import tempfile
import lvm
import math
import tzlocal
import logging
import json

TZ = tzlocal.get_localzone()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logging.getLogger('botocore').setLevel(logging.WARN)
logging.getLogger('boto3').setLevel(logging.WARN)
logging.getLogger('requests').setLevel(logging.WARN)
logger = logging.getLogger(__name__)


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
        choices=('dev', 'backup'),
        default='backup', help=('Choose backup here.')
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
    parser.add_argument(
        '--cloudwatch-log-group-name', dest='log_group_name',
        default=None, help=('CloudWatch log group name.')
    )
    return parser.parse_args()


class MongoBackups:
    def __init__(self, mongo_name, aws_region, vg_name, lv_name, **kwargs):
        self.mongo_name = mongo_name
        self.aws_region = aws_region
        self.vg_name = vg_name
        self.lv_name = lv_name

        # A dict which will hold stats that are added to the snapshot as tags
        # and will be used for reporting.
        self.stats = {}

        self.log_group_name = kwargs.get('log_group_name')
        self.log_next_sequence_token = None

    def log(self, message, console=True):
        """ Log message.

        By default, log message to the console. If console=False, no message
        will be sent to the console.

        If self.log_group_name has been set, attempt to create the log
        stream name for this backup. Once log stream name created, send
        message to stream.

        """

        if console:
            logger.info(message)
        if self.log_group_name and self.log_group_name:
            kwargs = {
                'logGroupName': self.log_group_name,
                'logStreamName': self.log_stream_name,
                'logEvents': [
                    {
                        'timestamp': int(time.time() * 1000),
                        'message': message,
                    }
                ],
            }
            if self.log_next_sequence_token:
                kwargs['sequenceToken'] = self.log_next_sequence_token
            response = self.logs_client.put_log_events(**kwargs)
            self.log_next_sequence_token = response['nextSequenceToken']

    @property
    def log_stream_name(self):
        """ The log stream name. """

        log_stream_name = None
        try:
            log_stream_name = self.__log_stream_name
        except AttributeError:
            # If user has specified a log_group_name and the log stream
            # hasnt been created, create one.
            if self.log_group_name:
                new_log_stream_name = (
                    '{0}-{1}-{2}'.
                    format(
                        self.mongo_name,
                        self.instance_id,
                        int((time.time() + 0.5) * 1000)
                    )
                )
                self.logs_client.create_log_stream(
                    logGroupName=self.log_group_name,
                    logStreamName=new_log_stream_name
                )
                self.log_stream_name = new_log_stream_name
                log_stream_name = self.log_stream_name
                self.log(
                    "Creating CloudWatch log stream [log_group={0}, "
                    "log_stream={1}].".
                    format(self.log_group_name, self.log_stream_name)
                )

        return log_stream_name

    @log_stream_name.setter
    def log_stream_name(self, log_stream_name):
        self.__log_stream_name = log_stream_name

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
    def logs_client(self):
        """ A client connection to CloudWatch Logs. """

        return self.session.client('logs', self.aws_region)

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
            self.logs(
                "Checking that snapshot is complete [{0}].".
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

        self.snapshot_tags = [
            {'Key': 'InstanceId', 'Value': self.instance_id},
            {'Key': 'Name', 'Value': name},
            {'Key': 'Description', 'Value': description},
            {'Key': 'MongoName', 'Value': self.mongo_name},
            {'Key': 'MongoBackups', 'Value': 'True'},
            {'Key': 'DateStarted', 'Value': self.stats['date_started']},
            {'Key': 'DateFinished', 'Value': self.stats['date_finished']},
            {'Key': 'MongoBackupsVersion', 'Value': __VERSION__}
        ]

        # append rsync stats to tags.
        self.snapshot_tags = self.snapshot_tags + self.stats['rsync_stats']

        resp = self.client.create_snapshot(
            Description=description, VolumeId=volume_id,
            TagSpecifications=[
                {
                    'ResourceType': 'snapshot',
                    'Tags': self.snapshot_tags
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

    def capture_rsync_stats(self, rsync_output):
        """ Take output from rsnapshot and store statistics in stats
            member. """

        rsync_output = rsync_output.split(b'\n')
        self.stats['rsync_stats'] = []

        for output_line in rsync_output:
            output_line = output_line.decode()
            output_line = output_line.replace(",", "")
            regex = (
                r'^(?P<key>(File|Number|Total|Literal|Matched)[a-z\s]+)'
                ':\s(?P<value>[0-9+|[0-9\.]+)'
            )
            found = re.search(regex, output_line)
            if found:
                _key = found.groupdict()['key']
                _value = found.groupdict()['value']
                _key = _key.lower().replace(' ', '_')
                self.stats['rsync_stats'].append(
                    {'Key': 'rsync_{0}'.format(_key), 'Value': _value}
                )
        return self.stats


def main():
    args = parse_args()

    mongo_backups = MongoBackups(
        args.mongo_name, args.aws_region, args.vg_name, args.lv_name,
        log_group_name=args.log_group_name
    )

    mongo_backups.stats['date_started'] = dt.now().isoformat()

    _filter = mongo_backups.volume_filter
    volumes = mongo_backups.client.describe_volumes(Filters=_filter)

    if args.action == 'backup':
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
                    mongo_backups.log(
                        "Creating a new volume from the last "
                        "snapshot [snapshot_id={0}, volume_type={1}]."
                        .format(
                            mongo_backups.last_snapshot['snapshot_id'],
                            volume_type
                        )
                    )
                    if not mongo_backups.last_snapshot['snapshot_id']:
                        mongo_backups.log("No snapshots exist yet.")
                        sys.exit(2)
                    else:
                        new_volume = mongo_backups.ebs_create_volume(
                            size=None, volume_type=volume_type,
                            snapshot_id=(
                                mongo_backups.last_snapshot['snapshot_id']
                            )
                        )
                else:
                    mongo_backups.log(
                        "Creating a new volume [size={0}GB, volume_type={1}]."
                        .format(size, volume_type)
                    )
                    new_volume = mongo_backups.ebs_create_volume(
                        size, volume_type
                    )

                # Wait for new volume to be available.
                mongo_backups.log(
                    "Waiting for new volume to become available [{0}]."
                    .format(new_volume['VolumeId'])
                )
                waiter = mongo_backups.client.get_waiter('volume_available')
                waiter.wait(VolumeIds=[new_volume['VolumeId']])
                mongo_backups.log(
                    "Volume available [{0}].".format(new_volume['VolumeId'])
                )

                last_block_device = mongo_backups.get_latest_block_device()
                mongo_backups.log(
                    "Last block device attached [{0}].".
                    format(last_block_device)
                )

                # Found next free block device.
                attach_device = mongo_backups.get_next_free_block_device()
                mongo_backups.log(
                    "Next available block device found [{0}].".
                    format(attach_device)
                )

                # Attach volume to instance.
                mongo_backups.log(
                    "Attaching volume [volume_id={0}, device={1}].".
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
                    mongo_backups.log(
                        "Waiting for new block device to attach [{0}]."
                        .format(attach_device)
                    )
                    if last_block_device != latest_block_device:
                        mongo_backups.log(
                            "New block device attached [{0}].".
                            format(latest_block_device)
                        )
                        break

                # Create a filesystem on the new block device.
                mongo_backups.log(
                    "Creating xfs filesystem [/dev/{0}].".
                    format(latest_block_device)
                )
                subprocess.call(
                    'mkfs.xfs /dev/{0}'.
                    format(latest_block_device),
                    shell=True
                )

                # Make a temporary mount points for the new volume and LVM
                # snapshot.
                mongo_backups.log("Creating temporary mount point directories.")
                temp_mount_point_new_volume = tempfile.mkdtemp(
                    prefix='/media/'
                )
                temp_mount_point_lvsnap = tempfile.mkdtemp(prefix='/media/')

                # Mount the new block device at temporary mount point.
                mongo_backups.log(
                    "Mounting new block device [dev=/dev/{0}, dest={1}].".
                    format(latest_block_device, temp_mount_point_new_volume)
                )
                subprocess.call(
                    'mount /dev/{0} {1}'.
                    format(latest_block_device, temp_mount_point_new_volume),
                    shell=True
                )

                # Lock mongo.
                if args.mongo_lock:
                    mongo_backups.log("Locking mongo.")
                    conn = MongoClient('mongodb://127.0.0.1:27017')
                    conn.fsync(lock=True)

                # Create LVM snapshot.
                mongo_backups.log(
                    "Creating LVM snapshot [vg={0}, lv={1}].".
                    format(mongo_backups.vg_name, mongo_backups.lv_name)
                )
                subprocess.call(
                    'lvcreate -L300M -s -n lvsnap '
                    '/dev/mapper/{0}-{1}'
                    .format(mongo_backups.vg_name, mongo_backups.lv_name),
                    shell=True
                )

                # Unlock mongo.
                if args.mongo_lock:
                    mongo_backups.log("Unlocking mongo.")
                    conn.unlock()

                # Mount LVM snapshot in read-only.
                lvm_snapshot_mount_args = 'nouuid,ro'
                mongo_backups.log(
                    "Mounting LVM snapshot [mount_args={0}, "
                    "dev=/dev/{1}/lvsnap, dest={2}].".
                    format(
                        lvm_snapshot_mount_args, mongo_backups.vg_name,
                        temp_mount_point_lvsnap
                    )
                )
                subprocess.call(
                    'mount -o nouuid,ro /dev/{0}/lvsnap {1}'.
                    format(mongo_backups.vg_name, temp_mount_point_lvsnap),
                    shell=True
                )

                # Rsync LVM snapshot to new volume.
                mongo_backups.log(
                    "Performing rsync [src={0}, dest={1}].".
                    format(temp_mount_point_lvsnap, temp_mount_point_new_volume)
                )
                rsync_output = subprocess.check_output(
                    'rsync -a --stats --delete --ignore-missing-args '
                    '-p {0}/* {1}/'.
                    format(
                        temp_mount_point_lvsnap,
                        temp_mount_point_new_volume
                    ),
                    shell=True
                )
                mongo_backups.capture_rsync_stats(rsync_output)

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
                mongo_backups.log(
                    "Creating snapshot from volume [{0}]."
                    .format(new_volume['VolumeId'])
                )
                snapshot = mongo_backups.ebs_create_snapshot(
                    new_volume['VolumeId']
                )

                # Detach the new volume.
                mongo_backups.log(
                    "Detaching volume which contains the database "
                    "backup [{0}].".
                    format(new_volume['VolumeId'])
                )
                mongo_backups.ebs_detach_volume(
                    new_volume['VolumeId'], attach_device
                )
                waiter = mongo_backups.client.get_waiter('volume_available')
                waiter.wait(VolumeIds=[new_volume['VolumeId']])
                mongo_backups.log(
                    "Volume detached [{0}].".format(new_volume['VolumeId'])
                )

                mongo_backups.log(
                    "Deleting new volume [{0}]."
                    .format(new_volume['VolumeId'])
                )
                mongo_backups.ebs_delete_volume(new_volume['VolumeId'])

                mongo_backups.log(
                    "Backup complete [snapshot_id={0}]."
                    .format(snapshot['SnapshotId'])
                )

                # Send snapshot tags to CloudWatch log stream.
                mongo_backups.log(
                    json.dumps(mongo_backups.snapshot_tags, indent=4),
                    console=False
                )

                sys.exit(0)


if __name__ == '__main__':
    sys.exit(main())
