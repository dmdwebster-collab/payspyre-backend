# AWS S3 Setup Guide

## Prerequisites

- AWS Account with appropriate IAM permissions
- AWS CLI installed and configured (optional)

## 1. Create S3 Bucket

```bash
aws s3 mb s3://payspyre-documents --region us-east-1
```

Or via AWS Console:
1. Go to S3 service
2. Click "Create bucket"
3. Bucket name: `payspyre-documents` (must be globally unique)
4. Region: `us-east-1`
5. Block public access: Enable all settings
6. Click "Create bucket"

## 2. Create IAM User for Application Access

### Option A: Using AWS CLI

```bash
# Create IAM user
aws iam create-user --user-name payspyre-backend

# Attach policy to user
aws iam attach-user-policy \
  --user-name payspyre-backend \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

# Create access key
aws iam create-access-key --user-name payspyre-backend
```

Save the `AccessKeyId` and `SecretAccessKey` from the output.

### Option B: Using AWS Console

1. Go to IAM service
2. Navigate to "Users" → "Add users"
3. User name: `payspyre-backend`
4. Select "Access key - Programmatic access"
5. Attach existing policy: `AmazonS3FullAccess`
6. Create user and save credentials

## 3. Configure Bucket Policy

Create a bucket policy that enforces encryption and SSL:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnencryptedObjectUploads",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::payspyre-documents/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "AES256"
        }
      }
    },
    {
      "Sid": "DenyInsecureConnections",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::payspyre-documents",
        "arn:aws:s3:::payspyre-documents/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
```

Apply via AWS Console or CLI:
```bash
aws s3api put-bucket-policy \
  --bucket payspyre-documents \
  --policy file://bucket-policy.json
```

## 4. Enable Bucket Versioning (Optional but Recommended)

```bash
aws s3api put-bucket-versioning \
  --bucket payspyre-documents \
  --versioning-configuration Status=Enabled
```

## 5. Configure Server-Side Encryption

```bash
aws s3api put-bucket-encryption \
  --bucket payspyre-documents \
  --server-side-encryption-configuration '{
    "Rules": [
      {
        "ApplyServerSideEncryptionByDefault": {
          "SSEAlgorithm": "AES256"
        }
      }
    ]
  }'
```

## 6. Set Up Lifecycle Rules for Document Expiration

Create a lifecycle rule to automatically delete documents after retention period:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket payspyre-documents \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "DeleteOldDocuments",
        "Status": "Enabled",
        "Filter": {
          "Prefix": ""
        },
        "Expiration": {
          "Days": 2555
        }
      }
    ]
  }'
```

This expires documents after 7 years (2555 days), which is typical for financial records.

## 7. Configure Environment Variables

Add these to your `.env` file:

```bash
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
AWS_S3_BUCKET=payspyre-documents
AWS_REGION=us-east-1
```

## 8. Test Configuration

Run the following to verify S3 connectivity:

```bash
python -c "
from app.services.storage import storage_service
print('S3 bucket:', storage_service._bucket_name)
print('Region:', storage_service._region)
print('Configured successfully!')
"
```

## Security Best Practices

1. **Rotate credentials regularly** - Update IAM keys quarterly
2. **Use MFA on IAM users** - Enable MFA for human access
3. **Monitor S3 access** - Enable CloudTrail logging
4. **Use bucket policies** - Enforce encryption and SSL
5. **Limit access by IP** - Restrict to your application's IPs
6. **Enable S3 Access Logs** - Track all bucket access
7. **Use S3 Object Lock** - For WORM (Write Once Read Many) compliance

## Troubleshooting

### Access Denied
- Verify IAM user has correct permissions
- Check bucket policy doesn't block your user
- Ensure credentials are correct

### Signature Does Not Match
- Verify `AWS_SECRET_ACCESS_KEY` is correct
- Check for whitespace in environment variables

### Bucket Not Found
- Verify `AWS_S3_BUCKET` name is correct
- Check bucket exists in specified region

### CORS Errors
- Configure CORS on S3 bucket for your frontend domain
```bash
aws s3api put-bucket-cors \
  --bucket payspyre-documents \
  --cors-configuration '{
    "CORSRules": [
      {
        "AllowedOrigins": ["https://your-frontend.com"],
        "AllowedMethods": ["GET"],
        "AllowedHeaders": ["*"]
      }
    ]
  }'
```