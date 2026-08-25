terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

variable "lambda_role_arn" { type = string }

locals {
  media_kinds = {
    image = { image_uri = "example/image-processor:2.1", batch_size = 10 }
    video = { image_uri = "example/video-processor:2.1", batch_size = 2 }
  }
}

resource "aws_s3_bucket" "raw" { bucket = "example-raw-media" }
resource "aws_s3_bucket" "processed" { bucket = "example-processed-media" }

resource "aws_dynamodb_table" "assets" {
  name         = "media-assets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "asset_id"
  attribute {
    name = "asset_id"
    type = "S"
  }
}

resource "aws_sns_topic" "media_created" { name = "media-created" }

resource "aws_sqs_queue" "dlq" {
  for_each = local.media_kinds
  name     = "${each.key}-jobs-dlq"
}

resource "aws_sqs_queue" "jobs" {
  for_each = local.media_kinds
  name     = "${each.key}-jobs"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

resource "aws_s3_bucket_notification" "raw_media" {
  bucket = aws_s3_bucket.raw.id
  topic {
    topic_arn = aws_sns_topic.media_created.arn
    events    = ["s3:ObjectCreated:*"]
  }
}

resource "aws_sns_topic_subscription" "jobs" {
  for_each     = local.media_kinds
  topic_arn    = aws_sns_topic.media_created.arn
  protocol     = "sqs"
  endpoint     = aws_sqs_queue.jobs[each.key].arn
  filter_policy = jsonencode({ kind = [each.key] })
}

resource "aws_lambda_function" "upload" {
  function_name = "create-media-upload"
  role          = var.lambda_role_arn
  package_type  = "Image"
  image_uri     = "example/create-media-upload:2.1"
  environment { variables = {
    RAW_BUCKET   = aws_s3_bucket.raw.id
    ASSETS_TABLE = aws_dynamodb_table.assets.name
  } }
}

resource "aws_lambda_function" "processor" {
  for_each      = local.media_kinds
  function_name = "${each.key}-processor"
  role          = var.lambda_role_arn
  package_type  = "Image"
  image_uri     = each.value.image_uri
  environment { variables = {
    OUTPUT_BUCKET = aws_s3_bucket.processed.id
    ASSETS_TABLE  = aws_dynamodb_table.assets.name
  } }
}

resource "aws_apigatewayv2_api" "media" {
  name = "media-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "upload" {
  api_id                 = aws_apigatewayv2_api.media.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.upload.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "uploads" {
  api_id    = aws_apigatewayv2_api.media.id
  route_key = "POST /uploads"
  target    = "integrations/${aws_apigatewayv2_integration.upload.id}"
}

resource "aws_lambda_event_source_mapping" "jobs" {
  for_each         = local.media_kinds
  event_source_arn = aws_sqs_queue.jobs[each.key].arn
  function_name    = aws_lambda_function.processor[each.key].arn
  batch_size       = each.value.batch_size
}

module "delivery" {
  source  = "terraform-aws-modules/cloudfront/aws"
  version = "3.4.0"
  enabled = true
  origin = {
    processed = { domain_name = aws_s3_bucket.processed.bucket_regional_domain_name }
  }
  default_cache_behavior = {
    target_origin_id       = "processed"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
  }
}
