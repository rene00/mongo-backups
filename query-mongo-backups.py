#!/usr/bin/env python3

__VERSION__ = '0.1'

import argparse
import boto3
import sys
import collections
import json


def tag_search(_item, _dict):
    """ Take a list of dicts and return a dict value. """

    found = [element for element in _dict if element['Key'] == _item]
    if found:
        found = found[0]['Value']
    return found


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
        '--limit', dest='limit', type=int, required=False, default=1,
        help=('The limit of backups to display.')
    )
    return parser.parse_args()


class QueryMongoBackups:
    def __init__(self, mongo_name, aws_region, limit):
        self.mongo_name = mongo_name
        self.aws_region = aws_region
        self.limit = limit

    @property
    def session(self):
        """ A session to AWS. """

        return boto3.session.Session()

    @property
    def client(self):
        """ A client connection to EC2. """

        return self.session.client('ec2', self.aws_region)

    @property
    def all_snapshots(self):
        """ Return an ordered dict (by date) of snapshots for mongo_name. """

        _filter = [
            {'Name': 'tag:MongoName', 'Values': [self.mongo_name]},
            {'Name': 'tag:MongoBackups', 'Values': ['True']},
        ]

        result = self.client.describe_snapshots(Filters=_filter)
        snapshots = {}

        for snapshot in result['Snapshots']:

            start_time = snapshot['StartTime']
            human_readable_start_time = start_time.isoformat()

            tags = snapshot['Tags']

            snapshot_data = {
                'Description': snapshot['Description'],
                'Encrypted': snapshot['Encrypted'],
                'Progress': snapshot['Progress'],
                'SnapshotId': snapshot['SnapshotId'],
                'DateStarted': tag_search('DateStarted', tags),
                'DateFinished': tag_search('DateFinished', tags),
                'MongoName': tag_search('MongoName', tags),
                'InstanceId': tag_search('InstanceId', tags),
            }

            rsync_stats = {}
            for tag in tags:
                if tag['Key'].startswith('rsync_'):
                    rsync_stats[tag['Key']] = tag['Value']

            # include all rsync stats
            data = {**snapshot_data, **rsync_stats}
            od = collections.OrderedDict(sorted(data.items()))
            snapshots[human_readable_start_time] = od

        snapshots_ordered_by_date = collections.OrderedDict()
        for key, value in sorted(
                snapshots.items(), key=lambda t: t[0], reverse=True):
            if len(snapshots_ordered_by_date) == self.limit:
                break
            snapshots_ordered_by_date[key] = value

        return snapshots_ordered_by_date


def main():

    if sys.version_info <= (3, 5):
        sys.exit('You must use python 3.5 or greater.')

    args = parse_args()

    mongo_backups = QueryMongoBackups(
        args.mongo_name, args.aws_region, args.limit
    )
    report = json.dumps(mongo_backups.all_snapshots, indent=4)
    print(report)


if __name__ == '__main__':
    sys.exit(main())
