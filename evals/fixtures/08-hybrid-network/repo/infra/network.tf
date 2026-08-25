resource "aws_vpc" "core" {
  cidr_block = "10.40.0.0/16"
}

resource "aws_subnet" "private" {
  vpc_id     = aws_vpc.core.id
  cidr_block = "10.40.10.0/24"
}

resource "aws_customer_gateway" "office" {
  bgp_asn    = 65010
  ip_address = var.office_gateway_ip
  type       = "ipsec.1"
}

resource "aws_vpn_gateway" "core" {
  vpc_id = aws_vpc.core.id
}

resource "aws_vpn_connection" "office" {
  customer_gateway_id = aws_customer_gateway.office.id
  vpn_gateway_id      = aws_vpn_gateway.core.id
  type                = "ipsec.1"
}
