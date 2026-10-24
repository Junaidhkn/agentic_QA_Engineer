# VPC with public subnets (NAT, load balancers) and private subnets (workloads) across AZs.
# NAT gateways use Elastic IPs so execution-plane egress comes from fixed, publishable addresses
# customers can allowlist (ADR-0010).

variable "name" { type = string }
variable "cidr" { type = string }
variable "azs" { type = list(string) }
variable "tags" {
  type    = map(string)
  default = {}
}
variable "create_data_subnets" {
  type        = bool
  default     = false
  description = "Isolated subnets (no internet route) for RDS and ElastiCache"
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(var.tags, { Name = var.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.azs[count.index]
  cidr_block              = cidrsubnet(var.cidr, 4, count.index)
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name}-public-${var.azs[count.index]}", Tier = "public" })
}

resource "aws_subnet" "private" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr, 4, count.index + 8)
  tags              = merge(var.tags, { Name = "${var.name}-private-${var.azs[count.index]}", Tier = "private" })
}

resource "aws_eip" "nat" {
  count  = length(var.azs)
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name}-nat-${var.azs[count.index]}" })
}

resource "aws_nat_gateway" "this" {
  count         = length(var.azs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = merge(var.tags, { Name = "${var.name}-${var.azs[count.index]}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(var.tags, { Name = "${var.name}-public" })
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = length(var.azs)
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }
  tags = merge(var.tags, { Name = "${var.name}-private-${var.azs[count.index]}" })
}

resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_subnet" "data" {
  count             = var.create_data_subnets ? length(var.azs) : 0
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr, 4, count.index + 4)
  tags              = merge(var.tags, { Name = "${var.name}-data-${var.azs[count.index]}", Tier = "data" })
}

resource "aws_route_table" "data" {
  count  = var.create_data_subnets ? 1 : 0
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-data" })
}

resource "aws_route_table_association" "data" {
  count          = var.create_data_subnets ? length(var.azs) : 0
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data[0].id
}

resource "aws_flow_log" "this" {
  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow.arn
  iam_role_arn         = aws_iam_role.flow.arn
}

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/vpc/${var.name}/flow"
  retention_in_days = 30
}

resource "aws_iam_role" "flow" {
  name = "${var.name}-flow-logs"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "vpc-flow-logs.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "flow" {
  role = aws_iam_role.flow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow.arn}:*"
    }]
  })
}

output "vpc_id" { value = aws_vpc.this.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "egress_ips" { value = aws_eip.nat[*].public_ip }
output "data_subnet_ids" { value = aws_subnet.data[*].id }
output "cidr" { value = var.cidr }
