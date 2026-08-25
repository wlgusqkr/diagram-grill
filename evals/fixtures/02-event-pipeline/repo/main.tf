terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_s3_bucket" "ingest" {
  bucket = "example-event-ingest"
}

resource "aws_s3_bucket_notification" "ingest_events" {
  bucket      = aws_s3_bucket.ingest.id
  eventbridge = true
}

resource "aws_cloudwatch_event_rule" "object_created" {
  name = "ingest-object-created"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [aws_s3_bucket.ingest.id]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "processor" {
  rule = aws_cloudwatch_event_rule.object_created.name
  arn  = aws_lambda_function.processor.arn
}

resource "aws_dynamodb_table" "jobs" {
  name         = "processed-object-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "object_key"

  attribute {
    name = "object_key"
    type = "S"
  }
}

resource "aws_iam_role" "processor" {
  name = "event-processor"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "processor_jobs" {
  role = aws_iam_role.processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.jobs.arn
    }]
  })
}

resource "aws_lambda_function" "processor" {
  function_name    = "object-event-processor"
  role             = aws_iam_role.processor.arn
  runtime          = "python3.12"
  handler          = "handler.main"
  filename         = "processor.zip"
  source_code_hash = filebase64sha256("processor.zip")

  environment {
    variables = {
      JOBS_TABLE = aws_dynamodb_table.jobs.name
    }
  }
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.object_created.arn
}
