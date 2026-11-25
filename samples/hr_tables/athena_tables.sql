-- NOTE: this file is a template. The live tables in igtl-hr-analytics-ayush-2026 use top-level
-- folders (s3://igtl-hr-analytics-ayush-2026/employees/, .../employee_education/), no hr/ prefix.
-- Synthetic HR analytics database
CREATE DATABASE IF NOT EXISTS hr_analytics;

-- Replace YOUR_BUCKET with your bucket name.
-- Run each CREATE EXTERNAL TABLE only after uploading its Parquet file.
CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employees (
  employee_id string, full_name string, work_email string, work_phone string,
  department string, designation string, manager_id string, employment_status string,
  work_location string, work_type string, date_of_birth date, gender string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employees/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employment_details (
  employee_id string, date_of_joining date, confirmation_date date, employment_type string,
  probation_end_date date, contract_end_date date, last_working_date date,
  notice_period_days int, exit_reason string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employment_details/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employee_education (
  education_id string, employee_id string, highest_qualification string, specialization string,
  institution string, graduation_year int, certifications string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employee_education/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employee_compensation (
  compensation_id string, employee_id string, annual_ctc_inr bigint, monthly_gross_inr double,
  basic_monthly_inr double, effective_from date, bonus_eligible string, salary_band string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employee_compensation/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employee_bank_details (
  employee_id string, bank_name string, account_number_placeholder string, ifsc_placeholder string,
  account_type string, verification_status string, effective_from date
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employee_bank_details/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.employee_sensitive_details (
  employee_id string, personal_email string, personal_phone_placeholder string, current_address string,
  pan_placeholder string, aadhaar_placeholder string, emergency_contact_name string,
  emergency_contact_phone_placeholder string, sensitive_data_note string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/employee_sensitive_details/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.leave_records (
  leave_id string, employee_id string, leave_type string, start_date date, end_date date,
  days_requested int, approval_status string, approver_employee_id string, reason_category string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/leave_records/';

CREATE EXTERNAL TABLE IF NOT EXISTS hr_analytics.attendance (
  attendance_id string, employee_id string, attendance_date date, check_in_time string,
  check_out_time string, attendance_status string, work_mode string
) STORED AS PARQUET LOCATION 's3://YOUR_BUCKET/hr/attendance/';

-- Example validation queries
SELECT department, COUNT(*) AS employee_count
FROM hr_analytics.employees
GROUP BY department ORDER BY employee_count DESC;

SELECT e.employee_id, e.full_name, e.designation, d.date_of_joining, c.annual_ctc_inr
FROM hr_analytics.employees e
LEFT JOIN hr_analytics.employment_details d ON e.employee_id = d.employee_id
LEFT JOIN hr_analytics.employee_compensation c ON e.employee_id = c.employee_id
WHERE e.employee_id = 'EMP001';
