# PDF Upload Open API

This API accepts a PDF file upload and authorizes the request using a merchant key passed in the `Authorization` header.

## Endpoint

`POST /tracking/openapi/pdf/upload/`

---

## Authentication

This endpoint requires a merchant authorization key.

### Supported header formats

#### Bearer format
```http
Authorization: Bearer <merchant_key>
```

#### Raw key format
```http
Authorization: <merchant_key>
```

If the authorization header is missing or invalid, the API returns `401 Unauthorized`.

---

## Request

### Content-Type

```http
multipart/form-data
```

### Form fields

| Field | Type | Required | Description |
|---|---|---:|---|
| `file` | file | Yes | PDF file to upload |

---

## Validation Rules

The API performs the following checks:

1. `Authorization` header must be present
2. Uploaded file must be provided
3. File name must end with `.pdf`
4. File content must not be empty

---

## Success Response

### Status
`200 OK`

### Response body
```json
{
  "ok": true,
  "message": "PDF accepted",
  "filename": "sample.pdf",
  "content_type": "application/pdf",
  "size_bytes": 245678,
  "pages": 12,
  "sha256": "6e8f0a8a3f6d9f7b3a4c8f0e1c2d3b4a5f6e7d8c9b0a1e2f3d4c5b6a7e8f9a0"
}
```

### Response fields

| Field | Type | Description |
|---|---|---|
| `ok` | boolean | Indicates request success |
| `message` | string | Success message |
| `filename` | string | Uploaded PDF filename |
| `content_type` | string | MIME type of uploaded file |
| `size_bytes` | integer | Size of uploaded file in bytes |
| `pages` | integer | Number of pages in the PDF |
| `sha256` | string | SHA-256 hash of the uploaded PDF bytes |

---

## Error Responses

### Missing authorization header
**Status:** `401 Unauthorized`

```json
{
  "ok": false,
  "error": "Authorization key is required"
}
```

### Invalid merchant key
**Status:** `401 Unauthorized`

```json
{
  "ok": false,
  "error": "Invalid merchant key"
}
```

### Non-PDF file uploaded
**Status:** `400 Bad Request`

```json
{
  "ok": false,
  "error": "only PDF file allowed"
}
```

### Empty file uploaded
**Status:** `400 Bad Request`

```json
{
  "ok": false,
  "error": "empty PDF file"
}
```

### Invalid PDF content
**Status:** `400 Bad Request`

```json
{
  "ok": false,
  "error": "invalid PDF: <details>"
}
```

---

## cURL Example

```bash
curl -X POST 'http://localhost:8000/tracking/openapi/pdf/upload/' \
  --header 'Authorization: Bearer YOUR_MERCHANT_KEY' \
  --form 'file=@"/path/to/sample.pdf"'
```

---

## Python Requests Example

```python
import requests

url = "http://localhost:8000/<your-pdf-upload-endpoint>/"
headers = {
    "Authorization": "Bearer YOUR_MERCHANT_KEY"
}

with open("sample.pdf", "rb") as f:
    files = {
        "file": ("sample.pdf", f, "application/pdf")
    }
    response = requests.post(url, headers=headers, files=files)

print(response.status_code)
print(response.json())
```

---

## Review Notes And Improvement Scope

These notes are based on the current implementation in `tracking/api/v1/validate.py`.
They do not replace the API contract above; they clarify what exists today and
what should be improved for long-term merchant automation.

### Current Automation Support

The current endpoint can be used for automation if a merchant has already
downloaded shipment-label PDFs from their shipping provider. A merchant script
can upload those PDFs to SecurePay one by one instead of manually entering AWB
and order details.

In the current implementation, each upload:

1. Accepts one PDF file.
2. Extracts courier, AWB, and order ID from the label where possible.
3. Checks whether the extracted order ID belongs to the authenticated merchant.
4. Creates a PDF validation record.
5. Processes the PDF in the background.
6. Creates or updates the shipment from extracted AWB/courier details.
7. Links the shipment to the matching SecurePay order.

Once the shipment is linked to the order, that order is no longer treated as
missing a shipment label.

### Important Implementation Difference

The current code validates the request using the merchant session token from
merchant login:

```http
Authorization: Bearer <merchant_session_token>
```

The documentation above mentions `merchant_key`. If merchant-key based machine
authentication is required for external automation, that should be implemented
explicitly instead of relying on the short-lived portal login session token.

### Scale Limitation

The current endpoint supports one PDF per request. For 1,000 PDFs, the merchant
would need to make 1,000 API calls.

That can work for a first integration if the merchant uploads with a small
parallel limit, for example 3 to 5 files at a time. It is not ideal as the final
scalable design because the current implementation starts processing with
`threading.Thread` inside the web process.

### Recommended Improvements

1. Add a bulk PDF upload endpoint:

```http
POST /tracking/openapi/pdf/bulk-upload/
```

It should accept multiple PDF files:

```text
files[]=label-1.pdf
files[]=label-2.pdf
files[]=label-3.pdf
```

or a ZIP:

```text
file=labels.zip
```

2. Add a batch status endpoint:

```http
GET /tracking/openapi/pdf/batches/{batch_id}/
```

It should show total files, processed files, successful links, failed files, and
per-file errors.

3. Move PDF processing to a real background queue such as Celery, RQ, or
Django-Q. This avoids losing processing work if the web server restarts.

4. Add long-lived scoped API keys for merchant system-to-system automation.
Portal session tokens are not ideal for automated integrations.

5. Add idempotency using PDF hash or a merchant-provided idempotency key, so
retrying an upload does not create duplicate records.

6. Improve duplicate detection. Current duplicate detection is based on file
name. For automation, duplicate checks should use merchant ID plus PDF hash,
AWB, or order ID.

7. Add optional merchant webhook callbacks so SecurePay can notify merchant
systems when a PDF is approved, rejected, or linked to an order.
