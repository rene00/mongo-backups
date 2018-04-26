#!/bin/bash

set -eux

PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin:/root/bin
INSTANCE_ID=`curl http://169.254.169.254/latest/meta-data/instance-id`


apt-get update -y

# Setup snapshot
apt-get install -y awscli jq
VOLUME_ID=$(
    aws ec2 --region=${region} create-volume --snapshot-id=${snapshot_id} \
            --availability-zone=${availability_zone} \
            --encrypted | jq .VolumeId | tr -d "'" | tr -d '"'
)

aws ec2 wait volume-available --region=${region} --volume-ids $VOLUME_ID
aws ec2 attach-volume --region=${region} \
    --volume-id $VOLUME_ID --instance-id $INSTANCE_ID --device=${device}
aws ec2 wait volume-in-use --region=${region} --volume-ids $VOLUME_ID
while test ! -b ${device}; do
    sleep 1
done
mkdir -p ${mount_point}
mount ${device} ${mount_point}

# Install salt-minion
wget -O - https://repo.saltstack.com/apt/ubuntu/16.04/amd64/latest/SALTSTACK-GPG-KEY.pub | sudo apt-key add -
echo 'deb http://repo.saltstack.com/apt/ubuntu/16.04/amd64/latest xenial main' > /etc/apt/sources.list.d/saltstack.list
apt-get install -y salt-minion python-git

while sleep 1; do
    test -f /srv/pillar/top.sls && break
done

cat - <<EOF > /etc/salt/minion
file_roots:
  base:
    - /srv/salt

pillar_roots:
  base:
    - /srv/pillar

fileserver_backend:
  - roots
  - git

gitfs_remotes:
  - https://github.com/rene00/mongodb-formula.git
EOF

apt install -y zip unzip awscli

# Do EBS volume attachment.

salt-call --local state.highstate -l info
