# HR Onboarding: deployment on AWS

Containers on ECS Fargate behind a load balancer. Each student's record is a document in **MongoDB**;
each student's uploaded documents live in their **own S3 folder**; the HR tables are written to the
**shared bucket `igtl-hr-analytics-ayush-2026`**, where the HR portal and Athena read them.

```
                    employees ──HTTPS──▶ ┌────────────────────┐   onboard.example.com
                                         │  Application Load  │──▶ ECS service "onboarding"  (2..6 containers, :8000)
                    HR team ───HTTPS──▶  │  Balancer (ALB)    │   hr.example.com
                                         └────────────────────┘──▶ ECS service "hr"          (1..3 containers, :8001)
                                                                        │             │
                         private subnets, outbound via NAT gateway      │             │
                                  ┌─────────────────────────────────────┘             └──────────────┐
                                  ▼                                                                  ▼
                      MongoDB Atlas  (database hr_onboarding)                    S3  (task role, no keys in the app)
                      applications  one document per student                       igtl-hr-analytics-ayush-2026   (shared, colleague's)
                      forms         the form as submitted                              <table>/<id>.parquet           the HR tables (top level)
                      counters      employee-ID counter                                onboarding/employees/<id>/...  (only if you use the shared bucket for documents)
                                                                                     your-private-bucket            (recommended: ID documents + sensitive tables)
                                                                                       onboarding/employees/<id>/documents/*
                                                                                       hr/employee_bank_details/, hr/employee_sensitive_details/
```

Every container is identical and keeps nothing on its own disk, so you can run as many as you need.

## 1. What is stored where

**One folder per student.** The ID (`IGTPL007`) is issued by an atomic counter in MongoDB, so two containers
can never give the same ID to two students. That ID is the student's MongoDB `_id` and their S3 folder name:

| Data | Where | Key / id |
|---|---|---|
| Whole application (personal, family, education, bank, references, policies, flags, status) | MongoDB `applications` | `_id = IGTPL007` |
| The form as submitted (so a flagged form can be reopened) | MongoDB `forms` | `_id = IGTPL007` |
| Uploaded documents: Aadhaar front/back, PAN, photo, education certificate, resume, bank proof, payslips, relieving letter | S3 | `onboarding/employees/IGTPL007/documents/<file>` |
| HR tables, one small Parquet file per student | S3 | `employees/IGTPL007.parquet`, `employee_education/...` (shared bucket, top level); `hr/employee_bank_details/...`, `hr/employee_sensitive_details/...` (your restricted bucket) |

The application record also stores `document_folder`, the S3 address of that student's folder.

The tables have the same columns as `Data/sources_hr/athena_tables.sql`. Rows are written when a student submits and
updated when HR approves, rejects or flags. `employee_compensation`, `employment_details`, `leave_records` and
`attendance` are not filled, because the form does not collect them. Columns HR fills in (`department`,
`work_email`, `manager_id` ...) are never overwritten by the app.

**How HR fetches:** the HR portal reads the records from MongoDB and the documents from S3, so it always shows the
latest data. For reports, query the tables with Athena on the same bucket.

## 2. Decisions to make first

### 2.1 Your colleague's bucket
Use it as it is, but find out four things from your colleague first:

1. **Which AWS account owns it?** If it is a *different* account, you need their permission twice: a bucket policy on
   their side (section 3.2) **and** the IAM policy on your side (section 7). Same account: only the IAM policy.
2. **Which region is it in?** `aws s3api get-bucket-location --bucket igtl-hr-analytics-ayush-2026`
3. **How is it encrypted?** SSE-S3 (default) works as is. If it uses their own KMS key, they must also allow your role
   in the key policy, and you set `S3_KMS_KEY_ID`.
4. **Is "Bucket owner enforced" on?** (S3 default.) Then files you write are owned by them, and their Athena can read them.

What the app does in that bucket: it **only writes its own files**: `employees/IGTPL###.parquet` and `employee_education/IGTPL###.parquet`
(top level, no `hr/` prefix, matching the live Athena tables; `SHARED_TABLE_PREFIX` changes this) (and, if you choose,
`onboarding/employees/...`). It never lists, overwrites or deletes your colleague's files.

### 2.2 Keep PAN, Aadhaar and bank numbers out of the shared bucket (strongly recommended)
Two things would otherwise be visible to everyone who can read your colleague's bucket or query their Athena tables:

- the uploaded ID documents (Aadhaar and PAN images), and
- the `employee_bank_details` and `employee_sensitive_details` tables. In those, the columns named `*_placeholder`
  hold the **real** PAN, Aadhaar, phone and account numbers.

So create **one private bucket of your own** (section 5) and set both `S3_DOCUMENTS_BUCKET` and
`S3_RESTRICTED_BUCKET` to it. The shared bucket then receives only `employees` and `employee_education`. If you do
put everything in the shared bucket, ask your colleague to limit who can read `onboarding/*` and those two table folders.

Also: your Athena users will see real students next to the 100 synthetic ones in `employees`
(filter with `employee_id LIKE 'IGTPL%'`).

### 2.3 Domain and certificate
You need a domain you control (e.g. `example.com`) for HTTPS: one name for employees (`onboard.example.com`) and one
for HR (`hr.example.com`). The HR portal carries a password and personal data, so do not run it over plain HTTP.

## 3. Before you start

You need: an AWS account, the **AWS CLI** and **Docker** installed, and `aws configure` done with your access keys.
Choose a region (use the bucket's region) and replace `REGION` and `ACCOUNT_ID` in every command below.

### 3.1 Check you can reach the shared bucket
```
aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/ --region REGION
```
A list (or an empty result) means yes. `AccessDenied` means ask your colleague for 3.2.

### 3.2 What your colleague adds (only if the bucket is in another account)
Send them this; they add it to the bucket policy of `igtl-hr-analytics-ayush-2026`. Fill in `YOUR_ACCOUNT_ID` after you
create the role in section 7:
```json
{
  "Sid": "HROnboardingApp",
  "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::YOUR_ACCOUNT_ID:role/hr-onboarding-task-role" },
  "Action": ["s3:GetObject", "s3:PutObject"],
  "Resource": [
    "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
    "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*",
    "arn:aws:s3:::igtl-hr-analytics-ayush-2026/onboarding/*"
  ]
}
```
plus one more statement allowing `s3:ListBucket` on `arn:aws:s3:::igtl-hr-analytics-ayush-2026` for the same principal.

## 4. MongoDB (Atlas)
1. Create an account at mongodb.com/atlas and a cluster in the same region as your AWS setup. Use at least an **M10**
   for real use (the free M0 is for trying only and has no backups).
2. *Database Access* → add a user (e.g. `hr_app`) with a strong password and the role **readWrite on `hr_onboarding`**.
3. *Network Access* → you will add the NAT gateway's IP in section 5. For now add your own IP, to test.
4. *Connect* → *Drivers* → copy the connection string:
   `mongodb+srv://hr_app:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`. This is `MONGODB_URI`.
   The collections (`applications`, `forms`, `counters`) are created automatically on the first submission.
5. Turn on *Backup* for the cluster.

## 5. AWS network, buckets and secrets

**Private bucket** (documents and sensitive tables), S3 console → *Create bucket*:
name e.g. `yourcompany-hr-private`, same region, **Block all public access = on**, **Bucket Versioning = on**,
default encryption **SSE-S3** (or KMS).

**Network** (VPC console → *Create VPC* → *VPC and more*): 2 availability zones, 2 public subnets, 2 private
subnets, **1 NAT gateway**, no S3 endpoint needed. Then:
- Copy the NAT gateway's **Elastic IP** into Atlas *Network Access* (step 4.3), and delete your own IP.
- Create security group **`alb-sg`**: inbound 80 and 443 from anywhere. (For the HR name you can instead allow only your
  office/VPN range with a second listener rule; see 8.5.)
- Create security group **`task-sg`**: inbound TCP 8000 and 8001 **from `alb-sg` only**; outbound all (it needs the
  internet for MongoDB Atlas and the Gemini API).

**Secrets** (Secrets Manager → *Store a new secret* → *Other type*, plain text), one each:

| Secret name | Value |
|---|---|
| `hr/mongodb_uri` | the `MONGODB_URI` from section 4 |
| `hr/gemini_api_key` | your Gemini key |
| `hr/hr_password` | the password for the HR portal login |

Note each secret's ARN.

## 6. Build and push the image
```
aws ecr create-repository --repository-name hr-onboarding --region REGION
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com
docker build -t ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/hr-onboarding:1 .
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/hr-onboarding:1
```
Use a new tag (`:2`, `:3` ...) for each release instead of `latest`, so you can roll back.

## 7. IAM roles
**Task execution role** `ecsTaskExecutionRole` (ECS creates it for you the first time; otherwise create it): attach
`AmazonECSTaskExecutionRolePolicy`, and add an inline policy so it can inject the three secrets:
```json
{ "Version": "2012-10-17", "Statement": [{ "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
  "Resource": ["SECRET_ARN_MONGODB", "SECRET_ARN_GEMINI", "SECRET_ARN_HR_PASSWORD"] }] }
```

**Task role** `hr-onboarding-task-role` (IAM → *Create role* → trusted entity *Elastic Container Service Task*). This is
what the app uses to reach S3. Inline policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SharedBucketOwnFiles", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
                   "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*",
                   "arn:aws:s3:::igtl-hr-analytics-ayush-2026/onboarding/*"] },
    { "Sid": "SharedBucketList", "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::igtl-hr-analytics-ayush-2026",
      "Condition": { "StringLike": { "s3:prefix": ["employees/*", "employee_education/*", "onboarding/*"] } } },
    { "Sid": "PrivateBucket", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::yourcompany-hr-private/*" },
    { "Sid": "PrivateBucketList", "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::yourcompany-hr-private" }
  ]
}
```
(If you use a KMS key, also allow `kms:GenerateDataKey` and `kms:Decrypt` on it.) The role has no delete permission on
the shared bucket's tables, on purpose.

## 8. Run it on ECS

### 8.1 Cluster and logs
ECS console → *Create cluster* → name `hr-onboarding`, infrastructure **AWS Fargate**. CloudWatch → *Log groups* →
create `/ecs/hr-onboarding`.

### 8.2 Task definition `onboarding`
ECS → *Task definitions* → *Create* → Fargate, **0.5 vCPU / 1 GB**, task role `hr-onboarding-task-role`, execution role
`ecsTaskExecutionRole`. One container, image `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/hr-onboarding:1`, port **8000**,
log driver `awslogs` with group `/ecs/hr-onboarding`. Leave the container command empty (the image runs the onboarding
portal). Environment and secrets:

| Type | Name | Value |
|---|---|---|
| env | `PORT` | `8000` |
| env | `STORAGE_BACKEND` | `mongo_s3` |
| env | `AWS_REGION` | `REGION` |
| env | `S3_BUCKET` | `igtl-hr-analytics-ayush-2026` |
| env | `S3_PREFIX` | `onboarding/` |
| env | `S3_DOCUMENTS_BUCKET` | `yourcompany-hr-private` |
| env | `S3_RESTRICTED_BUCKET` | `yourcompany-hr-private` |
| env | `MONGODB_DB` | `hr_onboarding` |
| env | `GEMMA_MODEL` | `gemma-4-26b-a4b-it` |
| secret | `MONGODB_URI` | ARN of `hr/mongodb_uri` |
| secret | `GEMINI_API_KEY` | ARN of `hr/gemini_api_key` |

### 8.3 Task definition `hr`
Same as above (0.25 vCPU / 0.5 GB is enough), but: port **8001**, **command override** `python,hr_info.py`, and:

| Type | Name | Value |
|---|---|---|
| env | `PORT` | `8001` |
| env | `HR_USER` | the login name, e.g. `hr` |
| env | `ONBOARDING_URL` | `https://onboard.example.com` (used in the link HR sends to a flagged student) |
| secret | `HR_PASSWORD` | ARN of `hr/hr_password` |
| (no `GEMINI_API_KEY`) | | |

The other variables (`STORAGE_BACKEND`, `AWS_REGION`, `S3_*`, `MONGODB_DB`, `MONGODB_URI`) are the same as in 8.2.

### 8.4 Certificate and load balancer
1. *Certificate Manager* → request a public certificate for `onboard.example.com` and `hr.example.com` (DNS validation).
2. EC2 → *Target groups* → create two, type **IP addresses**, protocol HTTP: `tg-onboarding` port 8000 and `tg-hr`
   port 8001, health check path **`/health`**, same VPC.
3. EC2 → *Load balancers* → *Create* **Application Load Balancer**, internet-facing, the two **public** subnets,
   security group `alb-sg`. Listener **HTTPS 443** with the certificate; default action: fixed response 404.
   Listener **HTTP 80**: redirect to HTTPS.
4. On the HTTPS listener add rules: host `onboard.example.com` → `tg-onboarding`; host `hr.example.com` → `tg-hr`.
5. Load balancer *Attributes* → **Idle timeout 120 seconds**. A document extraction can take longer than the 60 s default.
6. In your DNS, point both names (CNAME or Route 53 alias) at the load balancer's DNS name.

### 8.5 Services
In the cluster → *Create service* (launch type Fargate), twice:

| | `onboarding` | `hr` |
|---|---|---|
| Task definition | `onboarding` | `hr` |
| Desired tasks | **2** | **1** (2 for availability) |
| Networking | the two **private** subnets, `task-sg`, public IP **off** | same |
| Load balancing | ALB, target group `tg-onboarding`, container port 8000 | target group `tg-hr`, container port 8001 |

To let only your office reach the HR name: on the `hr.example.com` listener rule add a second condition *Source IP*
(your office/VPN range), and keep a catch-all rule below it that returns 403.

### 8.6 Autoscaling
Save as `scaling.json`:
```json
{ "TargetValue": 60.0,
  "PredefinedMetricSpecification": { "PredefinedMetricType": "ECSServiceAverageCPUUtilization" },
  "ScaleOutCooldown": 60, "ScaleInCooldown": 180 }
```
```
aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/onboarding --min-capacity 2 --max-capacity 6 --region REGION
aws application-autoscaling put-scaling-policy --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/onboarding --policy-name cpu60 --policy-type TargetTrackingScaling --target-tracking-scaling-policy-configuration file://scaling.json --region REGION
```
Repeat with `service/hr-onboarding/hr` and min 1 / max 3 if you want the HR portal to scale too. Containers are added when
average CPU passes 60 % and removed when it drops. Because the containers hold no state, adding or losing one never loses
a submission. The limits to watch as you grow are MongoDB (raise the Atlas tier) and the Gemini API quota.

## 9. Move your existing data in
Your current 9 applications live in the `submissions/`, `forms/` and `uploads/` folders. From the project folder, with
your Atlas IP allowance and AWS credentials in place (a temporary access key is fine), set these in the terminal and run:
```
set STORAGE_BACKEND=mongo_s3
set AWS_REGION=REGION
set S3_BUCKET=igtl-hr-analytics-ayush-2026
set S3_PREFIX=onboarding/
set S3_DOCUMENTS_BUCKET=yourcompany-hr-private
set S3_RESTRICTED_BUCKET=yourcompany-hr-private
set MONGODB_URI=mongodb+srv://...
python sync_tables.py --from-local
```
(PowerShell: `$env:NAME = "value"`.) It copies each student's record and form into MongoDB, each student's documents
into their S3 folder, and writes the table files. It is safe to run again. Later, `python sync_tables.py` alone rebuilds
all the tables from MongoDB.

## 10. Check that it works
1. Open `https://onboard.example.com`, fill a test application and submit.
2. MongoDB Atlas → *Browse Collections*: a new document in `applications` with your ID.
3. S3: `yourcompany-hr-private/onboarding/employees/<ID>/documents/` holds the uploads, and
   `igtl-hr-analytics-ayush-2026/employees/<ID>.parquet` exists.
4. Open `https://hr.example.com`, log in, and the application is listed with its documents.
5. Approve it in the HR portal: the status in `employees/<ID>.parquet` becomes `Approved - Joining`.
6. Athena: `SELECT employee_id, full_name, employment_status FROM hr_analytics.employees WHERE employee_id LIKE 'IGTPL%'`.
   (If your colleague's tables exist already, this works as is. Otherwise run `Data/sources_hr/athena_tables.sql` with
   the bucket name filled in.)
7. Stop one onboarding task in the ECS console: the site stays up and ECS starts a replacement.

## 11. Running it
- **Logs:** CloudWatch → `/ecs/hr-onboarding`. A failed table write is logged as `HR table sync failed` and the student's
  record is still saved; run `python sync_tables.py` to repair.
- **Releasing a new version:** build and push `:2`, make a new task definition revision, update the service. ECS
  replaces the containers one at a time with no downtime.
- **Backups:** Atlas backups cover the records; S3 versioning covers documents and tables.
- **Cost:** the fixed costs are the NAT gateway and the load balancer; the containers, MongoDB and S3 grow with use. Expect
  on the order of a hundred US dollars a month at small scale, and check it with the AWS Pricing Calculator.
- **Access:** give people their own IAM users or SSO, never share the root account, and turn on MFA.

## 12. Troubleshooting
| Symptom | Likely cause |
|---|---|
| Task stops straight away, log says `STORAGE_BACKEND=mongo_s3 needs MONGODB_URI` | the secret is not set on that task definition, or the execution role cannot read it |
| Log shows `ServerSelectionTimeoutError` | the NAT gateway's IP is not in Atlas *Network Access*, or the URI/password is wrong |
| Submit fails with `AccessDenied` on S3 | task role policy (section 7), or your colleague's bucket policy (3.2), or KMS key access |
| 502/504 from the load balancer | target group health check failing: check `task-sg` allows 8000/8001 from `alb-sg` and the path is `/health` |
| Extraction request times out | the ALB idle timeout is still 60 s (8.4 step 5), or the task has no internet route (NAT) |
| HR portal asks for a login you don't know | `HR_USER` and the `hr/hr_password` secret |
| Table files exist in S3 but not in Athena | the Glue tables are missing or point at another location; run `athena_tables.sql` |

## Appendix: all settings
| Variable | Meaning |
|---|---|
| `STORAGE_BACKEND` | `mongo_s3` (this deployment), `s3` (records as JSON in S3), `local` (default, files on disk) |
| `MONGODB_URI`, `MONGODB_DB` | MongoDB connection string (secret) and database name (default `hr_onboarding`) |
| `S3_BUCKET` | the shared bucket: only the `employees` and `employee_education` tables |
| `S3_DOCUMENTS_BUCKET` | **required**: your private bucket for uploaded documents; must differ from `S3_BUCKET` |
| `S3_RESTRICTED_BUCKET` | **required**: your private bucket for the bank and sensitive tables; must differ from `S3_BUCKET` and `S3_DOCUMENTS_BUCKET` |
| `S3_PREFIX` | prefix for the per-student folders, e.g. `onboarding/`; **required** with `STORAGE_BACKEND=s3` |
| `SHARED_TABLE_PREFIX` | folder before `employees/` and `employee_education/`; default empty (top level) |

The app refuses to start (with an error naming the variable) if any of these rules is broken.
| `S3_KMS_KEY_ID` | encrypt objects with this KMS key (SSE-S3 otherwise) |
| `AWS_REGION` | region of the buckets |
| `GEMINI_API_KEY`, `GEMMA_MODEL` | document extraction (onboarding portal only) |
| `HR_USER`, `HR_PASSWORD` | HR portal login (required on AWS) |
| `ONBOARDING_URL` | public address of the onboarding portal, used in the HR portal's flagged-application link |
| `HOST`, `PORT` | listen address (`0.0.0.0` in the image) and port (8000 / 8001) |
