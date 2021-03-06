#!/bin/bash

set -eux

PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin:/root/bin

apt-get update -y

for device in xvdb xvdc xvdd; do
    while test ! -b /dev/$device; do
        sleep 1
    done
done

mkswap /dev/xvdb
swapon /dev/xvdb

# LVM and mongodb mount point.
apt-get install lvm2 xfsprogs
pvcreate /dev/xvd[cd]
vgcreate vgmongo /dev/xvd[cd]
lvcreate --extents 80%FREE --name lvmongo vgmongo
mkfs.xfs /dev/vgmongo/lvmongo
mkdir -p /var/lib/mongodb
echo "/dev/vgmongo/lvmongo      /var/lib/mongodb        xfs     defaults        0       2" >> /etc/fstab
mount -av

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

# Call state.highstate.
salt-call --local state.highstate -l info

# Prepare for mongo-backups
pushd /root
apt-get install -y git virtualenvwrapper python3-lvm2
mkdir -p /opt/virtualenvs
virtualenv --python=`which python3` --system-site-packages \
    /opt/virtualenvs/mongo-backups
git clone https://github.com/rene00/mongo-backups.git
/opt/virtualenvs/mongo-backups/bin/pip3 install -r \
    /root/mongo-backups/requirements.txt
popd

# Generate some mongo data
while sleep 1; do
    if test -f /root/generate_data.sh; then
        bash -x /root/generate_data.sh
        break
    fi
done
