# Document Storage API

## Overview

The document storage system provides secure S3-backed file storage for KYC documents, PDF statements, and other sensitive files. All documents are stored in AWS S3 with automatic lifecycle management for compliance-driven retention policies.

## Architecture

- **Storage**: AWS S3 with server-side encryption (AES256)
- **Access**: Presigned URLs for secure uploads/downloads
- **Metadata**: PostgreSQL tracks document status, ownership, and lifecycle
- **Security**: IAM credentials, bucket policies, SSL enforcement

## Document Types

- `id_front` - Government ID front side
- `id_back` - Government ID back side
- `selfie` - Liveness verification selfie
- `proof_of_income` - Pay stubs, employment letters
- `bank_statement` - Bank account statements
- `tax_return` - Tax return documents
- `utility_bill` - Address verification
- `business_license` - Business registration
- `incorporation_document` - Articles of incorporation
- `shareholder_agreement` - Shareholder structure documents
- `other` - Miscellaneous documents

## API Endpoints

### 1. Initiate Document Upload

**POST** `/api/v1/documents/upload/initiate`

Request body:
```json
{
  "document_type": "id_front",
  "document_subtype": "passport",
  "title": "Passport Front",
  "description": "Primary borrower passport",
  "file_name": "passport_front.jpg",
  "file_content_type": "image/jpeg",
  "file_size_bytes": 2048576,
  "metadata": {
    "country": "CA",
    "expiry_date": "2028-05-14"
  },
  "tags": ["primary", "identity"],
  "loan_application_id": "uuid-of-loan-app",
  "borrower_id": "uuid-of-borrower"
}
```

Response:
```json
{
  "upload_url": "https://payspyre-documents.s3.amazonaws.com/...",
  "upload_fields": {
    "key": "kyc_sessions/abc123/id_front/20260514_def12345_passport_front.jpg",
    "Content-Type": "image/jpeg",
    ...
  },
  "document_id": "uuid-of-document",
  "object_key": "kyc_sessions/abc123/id_front/20260514_def12345_passport_front.jpg",
  "expires_in": 3600
}
```

**Usage**:
1. Call this endpoint to get presigned upload URL
2. Use the returned `upload_url` and `upload_fields` to upload file directly to S3
3. Call `/confirm` endpoint to mark upload complete

### 2. Confirm Document Upload

**POST** `/api/v1/documents/upload/confirm`

Request body:
```json
{
  "document_id": "uuid-of-document",
  "file_size_bytes": 2048576,
  "file_hash": "optional-sha256-hash"
}
```

Response: Full document object with `status: "uploaded"`

### 3. Get Document

**GET** `/api/v1/documents/{document_id}`

Returns document metadata and status.

### 4. List Documents

**GET** `/api/v1/documents`

Query parameters:
- `loan_application_id` (optional) - Filter by loan application
- `borrower_id` (optional) - Filter by borrower
- `vendor_id` (optional) - Filter by vendor
- `document_type` (optional) - Filter by document type
- `status` (optional) - Filter by status
- `page` (default: 1) - Page number
- `page_size` (default: 20, max: 100) - Items per page

Response:
```json
{
  "documents": [...],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

### 5. Generate Download URL

**POST** `/api/v1/documents/download`

Request body:
```json
{
  "document_id": "uuid-of-document",
  "expires_in": 3600
}
```

Response:
```json
{
  "download_url": "https://payspyre-documents.s3.amazonaws.com/...",
  "expires_in": 3600,
  "document": {...}
}
```

### 6. Get Document Metadata

**GET** `/api/v1/documents/{document_id}/metadata`

Returns S3 object metadata including content type, size, and last modified.

### 7. Verify Document

**POST** `/api/v1/documents/verify`

Request body:
```json
{
  "document_id": "uuid-of-document",
  "status": "verified",
  "notes": "Document verified against known format"
}
```

### 8. List Document Versions

**GET** `/api/v1/documents/{document_id}/versions`

Returns all versions of a document (if versioning enabled).

### 9. Delete Document

**DELETE** `/api/v1/documents/{document_id}`

Deletes document from S3 and database. Cannot delete verified documents.

## KYC Document Endpoints

### KYC Document Upload

**POST** `/api/v1/kyc/{session_id}/documents/upload/initiate`

Initiates upload for KYC session documents. Automatically links document to KYC session.

Request body:
```json
{
  "document_type": "selfie",
  "title": "Liveness Selfie",
  "file_name": "selfie.jpg",
  "file_content_type": "image/jpeg"
}
```

### List KYC Documents

**GET** `/api/v1/kyc/{session_id}/documents`

Returns all documents associated with a KYC session.

## Document Lifecycle

1. **uploading** - Upload initiated, presigned URL generated
2. **uploaded** - File confirmed uploaded to S3
3. **verified** - Document reviewed and verified
4. **rejected** - Document rejected during review

## Retention Policy

- Default retention: 7 years (2555 days)
- Configurable per document
- Auto-expires via S3 lifecycle rules
- Documents marked for deletion are removed from S3

## Security Features

- Server-side encryption (AES256) enforced
- SSL/TLS required for all access
- Presigned URLs expire after 1 hour
- IAM role-based access control
- Bucket policy restricts public access
- File hash verification on upload

## Error Codes

- `400` - Invalid request, document not ready, or verified documents cannot be deleted
- `401` - Unauthorized or invalid signature
- `404` - Document or session not found
- `500` - S3 service error or configuration issue

## Storage Service

The `StorageService` class provides direct S3 operations:

```python
from app.services.storage import storage_service

# Generate upload URL
presigned = storage_service.generate_presigned_upload_url(
    entity_type="kyc_sessions",
    entity_id="123",
    document_type="id_front",
    filename="test.jpg",
    content_type="image/jpeg"
)

# Generate download URL
url = storage_service.generate_presigned_download_url(object_key, expires_in=3600)

# Get object metadata
metadata = storage_service.get_object_metadata(object_key)

# Delete object
storage_service.delete_object(object_key)

# Verify object exists
exists = storage_service.verify_object_exists(object_key)
```

## Frontend Integration

### Upload Flow

```typescript
// 1. Initiate upload
const { upload_url, upload_fields, document_id } = await fetch('/api/v1/documents/upload/initiate', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    document_type: 'id_front',
    title: 'Passport Front',
    file_name: file.name,
    file_content_type: file.type,
    file_size_bytes: file.size,
    loan_application_id: loanAppId
  })
}).then(r => r.json());

// 2. Upload to S3 using presigned URL
const formData = new FormData();
Object.entries(upload_fields).forEach(([key, value]) => {
  formData.append(key, value);
});
formData.append('file', file);

await fetch(upload_url, {
  method: 'POST',
  body: formData
});

// 3. Confirm upload
await fetch('/api/v1/documents/upload/confirm', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    document_id,
    file_size_bytes: file.size
  })
});
```

### Download Flow

```typescript
// Generate download URL
const { download_url } = await fetch('/api/v1/documents/download', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    document_id: docId,
    expires_in: 3600
  })
}).then(r => r.json());

// Open in new tab or download
window.open(download_url, '_blank');
```