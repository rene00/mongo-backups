variable "ssh_public_key" {}
variable "my_ip" {}

provider "aws" {
  region = "ap-southeast-2"
}

resource "aws_key_pair" "backup" {
  key_name   = "backup"
  public_key = "${var.ssh_public_key}"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.5.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_internet_gateway" "main" {
  vpc_id = "${aws_vpc.main.id}"
}

resource "aws_route_table" "main" {
  vpc_id = "${aws_vpc.main.id}"
}

resource "aws_route" "main" {
  route_table_id         = "${aws_route_table.main.id}"
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = "${aws_internet_gateway.main.id}"
}

resource "aws_subnet" "public" {
  vpc_id            = "${aws_vpc.main.id}"
  cidr_block        = "10.5.0.0/24"
  availability_zone = "ap-southeast-2a"
}

resource "aws_route_table_association" "public" {
  subnet_id      = "${aws_subnet.public.id}"
  route_table_id = "${aws_route_table.main.id}"
}

resource "aws_main_route_table_association" "main" {
  vpc_id         = "${aws_vpc.main.id}"
  route_table_id = "${aws_route_table.main.id}"
}

resource "aws_security_group" "public" {
  name        = "public"
  description = "public"
  vpc_id      = "${aws_vpc.main.id}"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${var.my_ip}/32"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_network_interface" "customerA" {
  subnet_id       = "${aws_subnet.public.id}"
  security_groups = ["${aws_security_group.public.id}"]
}

resource "aws_network_interface" "customerB" {
  subnet_id       = "${aws_subnet.public.id}"
  security_groups = ["${aws_security_group.public.id}"]
}

resource "aws_eip" "customerA" {
  vpc               = true
  network_interface = "${aws_network_interface.customerA.id}"
}

resource "aws_eip" "customerB" {
  vpc               = true
  network_interface = "${aws_network_interface.customerB.id}"
}

data "template_file" "cloud_config_sh" {
  template = "${file("resources/cloud-config.sh")}"
}

data "template_file" "cloud_config_cfg" {
  template = "${file("resources/cloud-config.cfg")}"
}

data "template_cloudinit_config" "customer" {
  gzip          = false
  base64_encode = false

  part {
    filename     = "cloud-config.sh"
    content_type = "text/x-shellscript"
    content      = "${data.template_file.cloud_config_sh.rendered}"
  }

  part {
    filename     = "cloud-config.cfg"
    content_type = "text/cloud-config"
    content      = "${data.template_file.cloud_config_cfg.rendered}"
  }
}

data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name                  = "backup"
  assume_role_policy    = "${data.aws_iam_policy_document.backup_assume.json}"
  force_detach_policies = true
}

data "aws_iam_policy_document" "backup" {
  statement {
    actions = [
      "ec2:*",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_policy" "backup" {
  name   = "backup"
  policy = "${data.aws_iam_policy_document.backup.json}"
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = "${aws_iam_role.backup.name}"
  policy_arn = "${aws_iam_policy.backup.arn}"
}

resource "aws_iam_instance_profile" "backup" {
  name = "backup"
  role = "${aws_iam_role.backup.name}"
}

resource "aws_ebs_volume" "customerA_xvdb" {
  availability_zone = "ap-southeast-2a"
  size              = 10
  encrypted         = true
}

resource "aws_volume_attachment" "customerA_xvdb" {
  device_name = "/dev/xvdb"
  volume_id   = "${aws_ebs_volume.customerA_xvdb.id}"
  instance_id = "${aws_instance.customerA.id}"
}

resource "aws_ebs_volume" "customerA_xvdc" {
  availability_zone = "ap-southeast-2a"
  size              = 10
  encrypted         = true

  tags {
    MongoName       = "customerA"
    MongoLiveVolume = "True"
  }
}

resource "aws_volume_attachment" "customerA_xvdc" {
  device_name = "/dev/xvdc"
  volume_id   = "${aws_ebs_volume.customerA_xvdc.id}"
  instance_id = "${aws_instance.customerA.id}"
}

resource "aws_ebs_volume" "customerA_xvdd" {
  availability_zone = "ap-southeast-2a"
  size              = 10
  encrypted         = true

  tags {
    MongoName       = "customerA"
    MongoLiveVolume = "True"
  }
}

resource "aws_volume_attachment" "customerA_xvdd" {
  device_name = "/dev/xvdd"
  volume_id   = "${aws_ebs_volume.customerA_xvdd.id}"
  instance_id = "${aws_instance.customerA.id}"
}

resource "aws_ebs_volume" "customerB_xvdb" {
  availability_zone = "ap-southeast-2a"
  size              = 10
  encrypted         = true
}

resource "aws_volume_attachment" "customerB_xvdb" {
  device_name = "/dev/xvdb"
  volume_id   = "${aws_ebs_volume.customerB_xvdb.id}"
  instance_id = "${aws_instance.customerB.id}"
}

resource "aws_ebs_volume" "customerB_xvdc" {
  availability_zone = "ap-southeast-2a"
  size              = 30
  encrypted         = true

  tags {
    MongoName       = "customerB"
    MongoLiveVolume = "True"
  }
}

resource "aws_volume_attachment" "customerB_xvdc" {
  device_name = "/dev/xvdc"
  volume_id   = "${aws_ebs_volume.customerB_xvdc.id}"
  instance_id = "${aws_instance.customerB.id}"
}

resource "aws_ebs_volume" "customerB_xvdd" {
  availability_zone = "ap-southeast-2a"
  size              = 30
  encrypted         = true

  tags {
    MongoName       = "customerB"
    MongoLiveVolume = "True"
  }
}

resource "aws_volume_attachment" "customerB_xvdd" {
  device_name = "/dev/xvdd"
  volume_id   = "${aws_ebs_volume.customerB_xvdd.id}"
  instance_id = "${aws_instance.customerB.id}"
}

resource "aws_instance" "customerA" {
  instance_type           = "t2.micro"
  key_name                = "${aws_key_pair.backup.key_name}"
  monitoring              = true
  disable_api_termination = false
  ami                     = "ami-d38a4ab1"
  availability_zone       = "ap-southeast-2a"
  iam_instance_profile    = "${aws_iam_instance_profile.backup.name}"
  user_data               = "${data.template_cloudinit_config.customer.rendered}"

  network_interface {
    network_interface_id = "${aws_network_interface.customerA.id}"
    device_index         = 0
  }

  root_block_device {
    volume_size = 20
  }

  provisioner "file" {
    source      = "salt/salt"
    destination = "/srv/"
  }

  provisioner "file" {
    source      = "salt/pillar"
    destination = "/srv/"
  }

  lifecycle {
    ignore_changes = ["user_data"]
  }
}

resource "aws_instance" "customerB" {
  instance_type           = "t2.medium"
  key_name                = "${aws_key_pair.backup.key_name}"
  monitoring              = true
  disable_api_termination = false
  ami                     = "ami-d38a4ab1"
  availability_zone       = "ap-southeast-2a"
  iam_instance_profile    = "${aws_iam_instance_profile.backup.name}"
  user_data               = "${data.template_cloudinit_config.customer.rendered}"

  network_interface {
    network_interface_id = "${aws_network_interface.customerB.id}"
    device_index         = 0
  }

  root_block_device {
    volume_size = 20
  }

  provisioner "file" {
    source      = "salt/salt"
    destination = "/srv/"
  }

  provisioner "file" {
    source      = "salt/pillar"
    destination = "/srv/"
  }

  provisioner "file" {
    source      = "resources/generate_data.sh"
    destination = "/root/generate_data.sh"
  }

  lifecycle {
    ignore_changes = ["user_data"]
  }
}


output "customerA_public_ip" {
  value = "${aws_instance.customerA.public_dns}"
}

output "customerB_public_ip" {
  value = "${aws_instance.customerB.public_dns}"
}
