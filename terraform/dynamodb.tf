# Seller allow-list. The partition key is the seller's AWS account ID
# (proposerAccountId). Seed it with scripts/seed_sellers.py --table-name.
resource "aws_dynamodb_table" "allowed_sellers" {
  #checkov:skip=CKV_AWS_119:Encrypted with the AWS managed DynamoDB key; the table holds seller IDs and grant ARNs, not secrets.
  name         = local.names.seller_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "proposerAccountId"

  attribute {
    name = "proposerAccountId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# Grants that License Manager was still processing when the handler first ran.
# The scheduled retry rule re-checks these and activates them once DISABLED.
resource "aws_dynamodb_table" "pending_grants" {
  #checkov:skip=CKV_AWS_119:Encrypted with the AWS managed DynamoDB key; the table holds seller IDs and grant ARNs, not secrets.
  name         = local.names.pending_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "grantArn"

  attribute {
    name = "grantArn"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}
