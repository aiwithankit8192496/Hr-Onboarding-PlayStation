# Synthetic Enterprise HR Dataset

All records are fictional. No real employee records were copied.

## Tables and approximate row counts
- employees: 100
- employment_details: 100
- employee_education: 100
- employee_compensation: 100
- employee_bank_details: 100
- employee_sensitive_details: 100
- leave_records: variable (1-3 per employee)
- attendance: 1,000 (10 sample days per employee)

## Safety
- Bank account, IFSC, PAN, Aadhaar, and phone values are explicitly synthetic placeholders.
- Do not treat placeholder identifiers as real or valid.
- `employee_sensitive_details` and `employee_bank_details` should be stored in separate restricted S3 prefixes/buckets and accessed only through an authorized backend.
- Do not rely on the LLM to enforce row-level security. Resolve identity and role from trusted authentication context and enforce authorization before returning rows.

## Convert CSVs to Parquet
Install dependencies in your environment:
    python -m pip install pandas pyarrow
Then run:
    python convert_to_parquet.py
Outputs will be in `parquet/`, one Snappy-compressed Parquet file per table.

## Suggested S3 prefixes
Shared bucket (igtl-hr-analytics-ayush-2026): the live Athena tables use top-level folders, no `hr/` prefix.
s3://SHARED_BUCKET/employees/
s3://SHARED_BUCKET/employee_education/

Restricted bucket (your own private bucket, never the shared one):
s3://RESTRICTED_BUCKET/hr/employee_bank_details/
s3://RESTRICTED_BUCKET/hr/employee_sensitive_details/

Tables the onboarding app does not write (employment_details, employee_compensation, leave_records,
attendance): put them wherever your Athena setup expects them.

## Athena / Glue notes
Create a Glue Catalog table per dataset with matching schema and S3 location. Keep restricted tables in separate locations and use IAM/Lake Formation permissions plus backend authorization.
