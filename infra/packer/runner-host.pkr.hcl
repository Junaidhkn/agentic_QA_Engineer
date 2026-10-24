# Runner host AMI: Amazon Linux 2023 + Docker + gVisor (runsc) + pre-pulled runner images.
# Rebuilt weekly (security patches) and rolled with ASG instance refresh.
packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1.3"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "k6_image" { type = string }
variable "playwright_image" { type = string }
variable "egress_proxy_image" { type = string }
variable "runner_agent_image" { type = string }

source "amazon-ebs" "runner" {
  region        = var.region
  instance_type = "c7i.large"
  ami_name      = "re-runner-{{timestamp}}"
  source_ami_filter {
    filters     = { name = "al2023-ami-2023.*-x86_64", virtualization-type = "hvm" }
    owners      = ["amazon"]
    most_recent = true
  }
  ssh_username = "ec2-user"
  metadata_options {
    http_tokens = "required"
  }
}

build {
  sources = ["source.amazon-ebs.runner"]
  provisioner "shell" {
    inline = [
      "sudo dnf -y update && sudo dnf -y install docker iptables-services",
      "ARCH=$(uname -m); URL=https://storage.googleapis.com/gvisor/releases/release/latest/$ARCH",
      "curl -fsSLO $URL/runsc -fsSLO $URL/runsc.sha512 && sha512sum -c runsc.sha512",
      "sudo install -m 0755 runsc /usr/local/bin/runsc && sudo /usr/local/bin/runsc install",
      "echo '{\"default-runtime\": \"runsc\", \"icc\": false, \"no-new-privileges\": true, \"log-driver\": \"json-file\", \"log-opts\": {\"max-size\": \"20m\"}}' | sudo tee /etc/docker/daemon.json",
      "sudo systemctl enable docker && sudo systemctl start docker",
      "sudo docker pull ${var.k6_image} && sudo docker pull ${var.playwright_image}",
      "sudo docker pull ${var.egress_proxy_image} && sudo docker pull ${var.runner_agent_image}",
    ]
  }
  # Runner agent unit (graceful drain on stop) and the host agent (slot metrics, lifecycle drain).
  provisioner "file" {
    sources     = ["files/runner-agent.service", "files/runner-host-agent.service", "files/runner-host-agent.sh"]
    destination = "/tmp/"
  }
  provisioner "shell" {
    inline = [
      "sudo install -m 0644 /tmp/runner-agent.service /tmp/runner-host-agent.service /etc/systemd/system/",
      "sudo install -m 0755 /tmp/runner-host-agent.sh /usr/local/bin/runner-host-agent.sh",
      "sudo mkdir -p /etc/systemd/system/runner-agent.service.d",
      "printf '[Service]\\nEnvironment=RUNNER_AGENT_IMAGE=${var.runner_agent_image}\\n' | sudo tee /etc/systemd/system/runner-agent.service.d/image.conf",
      "sudo systemctl daemon-reload",
    ]
  }
}
