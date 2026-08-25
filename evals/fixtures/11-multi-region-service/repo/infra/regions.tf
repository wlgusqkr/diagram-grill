module "service_us" {
  source = "../modules/regional-service"
  region = "us-east-1"
}

module "service_eu" {
  source = "../modules/regional-service"
  region = "eu-west-1"
}

resource "aws_route53_record" "service_us" {
  name           = "service.example.invalid"
  type           = "A"
  set_identifier = "us-east-1"
  records        = [module.service_us.endpoint]
  latency_routing_policy {}
}

resource "aws_route53_record" "service_eu" {
  name           = "service.example.invalid"
  type           = "A"
  set_identifier = "eu-west-1"
  records        = [module.service_eu.endpoint]
  latency_routing_policy {}
}
