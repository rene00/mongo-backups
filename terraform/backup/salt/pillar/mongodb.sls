## For versions of Mongo that use the YAML format for configuration, use the
## following. All entries in mongod_settings are written to the config file
## verbatim. The storage:dbPath and systemLog:path entries are required in
## this usage and take precedence over db_path at the top level (see references
## in mongodb/init.sls).
mongodb:
  use_repo: True
  version: 3.2 # use oldstable in for 1.8 - 2.6
  repo_component: multiverse    # this is for Ubuntu, use 'main' for Debian
  mongodb_package: mongodb-org
  mongodb_user: mongodb
  mongodb_group: mongodb
  mongod: mongod
  conf_path: /etc/mongod.conf
  mongod_settings:
    systemLog:
      destination: file
      logAppend: true
      path: /var/log/mongodb/mongod.log
    storage:
      dbPath: /var/lib/mongodb
      journal:
        enabled: true
    net:
      port: 27017
      bindIp: 0.0.0.0
    setParameter:
      textSearchEnabled: true

## Use this for MongoDB 2.4
# mongodb:
#   use_repo: False
#   mongodb_package: mongodb
#   conf_path: /etc/mongodb.conf
#   db_path: /mongodb/data
#   log_path: /mongodb/log
#   settings:
#     log_append: True
#     bind_ip: 0.0.0.0
#     port: 27017
#     journal: True
#     configsvr: False
#     shardsvr: True
#     replSet: squiggles
#     rest: False
#     set_parameter:
#       textSearchEnabled: 'true'

## Use this for MongoDB 3.0 on Ubuntu
# mongodb:
#   version: 3.0
#   keyid: 7F0CEB10 

## MongoDB query router configuration
mongos:
  use_repo: True
  mongos_package: mongodb-org-mongos
  log_path: /mongodb/log
  log_file: /mongodb/log/mongos.log
  settings:
    config_svrs: "cfg1.local:27019,cfg2.local:27019,cfg3.local:27019"
