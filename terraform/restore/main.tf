variable "ssh_public_key" {}
variable "my_ip" {}
variable "snapshot_id" {}

provider "aws" {
  region = "ap-southeast-2"
}

resource "aws_key_pair" "restore" {
  key_name   = "restore"
  public_key = "${var.ssh_public_key}"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.4.0.0/16"
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
  cidr_block        = "10.4.0.0/24"
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

resource "aws_network_interface" "restore" {
  subnet_id       = "${aws_subnet.public.id}"
  security_groups = ["${aws_security_group.public.id}"]
}

resource "aws_eip" "restore" {
  vpc               = true
  network_interface = "${aws_network_interface.restore.id}"
}

data "template_file" "cloud_config_sh" {
  template = "${file("resources/cloud-config.sh")}"

  vars {
    snapshot_id       = "${var.snapshot_id}"
    availability_zone = "ap-southeast-2a"
    region            = "ap-southeast-2"
    device            = "/dev/xvdc"
    mount_point       = "/var/lib/mongodb"
  }
}

data "template_file" "cloud_config_cfg" {
  template = "${file("resources/cloud-config.cfg")}"
}

data "template_cloudinit_config" "restore" {
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

data "aws_iam_policy_document" "restore_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "restore" {
  name                  = "restore"
  assume_role_policy    = "${data.aws_iam_policy_document.restore_assume.json}"
  force_detach_policies = true
}

data "aws_iam_policy_document" "restore" {
  statement {
    actions = [
      "ec2:*",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_policy" "restore" {
  name   = "restore"
  policy = "${data.aws_iam_policy_document.restore.json}"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = "${aws_iam_role.restore.name}"
  policy_arn = "${aws_iam_policy.restore.arn}"
}

resource "aws_iam_instance_profile" "restore" {
  name = "restore"
  role = "${aws_iam_role.restore.name}"
}

resource "aws_instance" "restore" {
  instance_type           = "t2.medium"
  key_name                = "${aws_key_pair.restore.key_name}"
  monitoring              = true
  disable_api_termination = false
  ami                     = "ami-d38a4ab1"
  availability_zone       = "ap-southeast-2a"
  iam_instance_profile    = "${aws_iam_instance_profile.restore.name}"
  user_data               = "${data.template_cloudinit_config.restore.rendered}"

  network_interface {
    network_interface_id = "${aws_network_interface.restore.id}"
    device_index         = 0
  }

  root_block_device {
    volume_size = 20
  }

  ebs_block_device {
    device_name = "/dev/xvdb"
    volume_size = 5
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

output "restore_public_ip" {
  value = "${aws_instance.restore.public_dns}"
}
