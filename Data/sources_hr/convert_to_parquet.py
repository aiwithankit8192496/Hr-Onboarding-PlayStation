"""
Convert the included synthetic HR CSVs to Parquet for S3 + Glue + Athena.
Run from this folder after: python -m pip install pandas pyarrow
"""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "parquet"
OUT.mkdir(exist_ok=True)

STRING_COLUMNS = {
    "employees": ["employee_id","full_name","work_email","work_phone","department","designation","manager_id","employment_status","work_location","work_type","date_of_birth","gender"],
    "employment_details": ["employee_id","date_of_joining","confirmation_date","employment_type","probation_end_date","contract_end_date","last_working_date","exit_reason"],
    "employee_education": ["education_id","employee_id","highest_qualification","specialization","institution","certifications"],
    "employee_compensation": ["compensation_id","employee_id","effective_from","bonus_eligible","salary_band"],
    "employee_bank_details": ["employee_id","bank_name","account_number_placeholder","ifsc_placeholder","account_type","verification_status","effective_from"],
    "employee_sensitive_details": ["employee_id","personal_email","personal_phone_placeholder","current_address","pan_placeholder","aadhaar_placeholder","emergency_contact_name","emergency_contact_phone_placeholder","sensitive_data_note"],
    "leave_records": ["leave_id","employee_id","leave_type","start_date","end_date","approval_status","approver_employee_id","reason_category"],
    "attendance": ["attendance_id","employee_id","attendance_date","check_in_time","check_out_time","attendance_status","work_mode"],
}
INTEGER_COLUMNS = {
    "employee_compensation": ["annual_ctc_inr"],
    "employee_education": ["graduation_year"],
    "leave_records": ["days_requested"],
    "employment_details": ["notice_period_days"],
}
FLOAT_COLUMNS = {"employee_compensation": ["monthly_gross_inr","basic_monthly_inr"]}
DATE_COLUMNS = {
    "employees": ["date_of_birth"],
    "employment_details": ["date_of_joining","confirmation_date","probation_end_date","contract_end_date","last_working_date"],
    "employee_compensation": ["effective_from"],
    "employee_bank_details": ["effective_from"],
    "leave_records": ["start_date","end_date"],
    "attendance": ["attendance_date"],
}

for csv_path in sorted(ROOT.glob("*.csv")):
    name = csv_path.stem
    df = pd.read_csv(csv_path, dtype="string", keep_default_na=False)
    for col in INTEGER_COLUMNS.get(name, []):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in FLOAT_COLUMNS.get(name, []):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in DATE_COLUMNS.get(name, []):
        df[col] = pd.to_datetime(df[col].replace("", pd.NA), errors="coerce").dt.date
    # Use nullable strings for identifiers and placeholders to preserve leading zeros and text.
    for col in STRING_COLUMNS.get(name, []):
        df[col] = df[col].astype("string")
    target = OUT / f"{name}.parquet"
    df.to_parquet(target, engine="pyarrow", index=False, compression="snappy")
    print(f"{target.name}: {len(df)} rows")
print(f"Done. Parquet files are in: {OUT}")
