#!/bin/bash

ROUNDS=$1
test -z "${ROUNDS}" && ROUNDS=2

pushd /root
git clone https://github.com/ozlerhakan/mongodb-json-files.git
pushd mongodb-json-files/datasets
echo mongo mydb --eval "db.dropDatabase()"
x=1
while [ $x -le ${ROUNDS} ]; do
  for i in *.json; do
        collection=$(echo $i | cut -d . -f 1)
        echo mongoimport -d mydb -c ${collection}${x} --file ${i}
  done
  x=$(( $x + 1 ))
done
popd
rm -rf mongodb-json-files
popd
