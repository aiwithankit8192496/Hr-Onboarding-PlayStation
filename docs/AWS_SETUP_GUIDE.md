# AWS Setup Guide: HR Onboarding (Serverless: Lambda, DynamoDB, S3, CloudFront, Company IAM User)

Written for someone new to AWS who has been given a **company IAM user** (not the root account). It matches this project's code (`Dockerfile`, `storage.py`, `jsonlog.py`, `employee_portal.py`, `hr_portal.py`, `hr_tables.py`, `sync_tables.py`, `scripts/migrate_mongo_to_dynamodb.py`, `hr_login.py`, `deploy/cloudfront/portal-viewer-request.js`, `deploy/cloudfront/hr-viewer-request.js`).

**What this version is**

- **Serverless, pay only for what is used.** The app runs as **two AWS Lambda functions** (`hr-onboarding-portal` for employees and `hr-onboarding-hr` for HR), both from **one Docker image** (arm64, with the AWS Lambda Web Adapter). Records are in **DynamoDB**. Files are in **S3**. **CloudFront** gives HTTPS and is the **only** way in. There is **no server to patch, no load balancer and no NAT gateway**.
- **It scales by itself.** Lambda adds copies when more people arrive and removes them when nobody is using the app. DynamoDB is "on demand". Nobody has to do anything.
- **It is off from 12:00 am to 7:00 am India time, every day, by itself** (Step 19). Two layers: a small **CloudFront Function** on each address answers every request in those hours with a friendly page ("The portal is available 7 am - 12 am IST", HTTP 503) before Lambda is even called, and behind it a schedule sets each function's reserved concurrency to 0 at midnight (and removes it at 7 am) as the **hard off switch**.
- **Where the data lives (`STORAGE_BACKEND=dynamodb_s3`):**
  - **DynamoDB** holds each employee's application (table `hr-onboarding-applications`, key `employee_id`), the form as submitted (`hr-onboarding-forms`) and the ID counter (`hr-onboarding-counters`). New IDs come from an atomic counter, so **two people never get the same ID**, and a record is only ever created with a "create only if it does not exist" write, so **nothing is overwritten**.
  - **S3** holds the uploaded documents in each employee's own folder `onboarding/employees/<ID>/documents/` (your private documents bucket), the bank and sensitive tables (your private restricted bucket), and the two shared HR tables `employees/` and `employee_education/` in your colleague's analytics bucket. **The browser uploads each file straight to S3** with a pre-signed link (into a waiting folder, `onboarding/pending/<random id>/`), so the files never pass through Lambda. The employee ID is claimed only when the application is submitted.
- **Privacy first.** PAN, Aadhaar, bank, phone, address and the scans are encrypted, locked to two roles, logged, and never written to log files (Part 7.2 is the full privacy section).
- **Logging and alerts come first (Part 3)**, before any employee data exists. When something breaks, go straight to **Part 9**.
- The earlier design (one EC2 server, MongoDB Atlas) was removed from the project; it is in the git history (commit `18bb9f3`, folder `archive/`). MongoDB is gone from this guide except the one-time data move in Step 20.

**About prices in this guide**

- Prices are **Mumbai (`ap-south-1`) prices in US dollars, before 18 % GST**. The S3, KMS, CloudWatch, SNS, CloudTrail, Athena, Glue and Route 53 figures were read from the official AWS Price List files on 2026-11-11 (Appendix B). **The Lambda, DynamoDB, CloudFront and ECR figures, and every total, are planning estimates that were not re-read from the price files**, and the line says so. Confirm everything in the AWS Pricing Calculator (https://calculator.aws) before you commit.
- A month is counted as 30 days.

Replace these placeholders everywhere (the project's real values are in the second column):

| Placeholder | Value for this project | Meaning |
|---|---|---|
| `<REGION>` | `ap-south-1` | Mumbai. Use the **same region for every service** (CloudFront certificates are the one exception: `us-east-1`) |
| `<ACCOUNT_ID>` | `503869803380` | The company AWS account number |
| `<SHARED_BUCKET>` | `igtl-hr-analytics-ayush-2026` | Your colleague's analytics bucket. **Fixed: do not create it, only add files.** Athena database `hr_analytics` reads its top-level folders `employees/` and `employee_education/` |
| `<BUCKET>` | `igtpl-hr-onboarding-docs-2026` | **Your** private bucket for the uploaded documents (Aadhaar, PAN, photo, resume...) |
| `<BUCKET_RESTRICTED>` | `igtpl-hr-onboarding-restricted-2026` | **Your** private bucket for the bank and sensitive tables only |
| `<BUCKET_LOGS>` | `hr-onboarding-system-logs-2026` | Logs only |
| `<KMS_KEY_ARN>` | `arn:aws:kms:ap-south-1:503869803380:key/...` | Your encryption key, alias `alias/hr-onboarding` (Step 9) |
| `<ECR_URI>` | `503869803380.dkr.ecr.ap-south-1.amazonaws.com/hr-onboarding` | The image repository (Step 14) |
| `<ONBOARDING_DOMAIN>`, `<HR_DOMAIN>` | `onboard.example.com`, `hr.example.com` | Two web addresses you control (ask the company IT team) |
| `<CERT_ARN>` | `arn:aws:acm:us-east-1:...` | The HTTPS certificate for the two names, **created in `us-east-1`** (Step 18) |
| `<PORTAL_DIST_ID>`, `<HR_DIST_ID>` | `E1ABC...` | The two CloudFront distributions (Step 18) |
| `<OFFICE_IPS>` | `203.0.113.10` | The office's public IPv4 address(es), listed at the top of the CloudFront Function (Step 18) |
| `<IAM_USER>` | `ankit.kumar` | Your company IAM user name |

Fixed names used below: CloudTrail trail **`hr-onboarding-audit-trail`**, SNS topic **`hr-onboarding-alerts`**, KMS alias **`alias/hr-onboarding`**, roles **`hr-onboarding-portal-role`**, **`hr-onboarding-hr-role`**, **`hr-onboarding-scheduler-role`**.

Bucket names are global across all of AWS. The shared bucket name is your colleague's.

---

## Part 0. First: what you can and cannot do with a company IAM user

### Step 0.1. Sign in and secure your user

- Get from the administrator: the **account ID or sign-in URL** (looks like `https://<ACCOUNT_ID>.signin.aws.amazon.com/console`), your **user name**, and your first password.
- Sign in, then change the password when asked. Choose at least 14 characters.
- Turn on **MFA** (a second code from your phone): account menu (top-right) → **Security credentials** → **Assign MFA device** → Authenticator app → scan the QR code → enter two consecutive codes. If MFA is already required by the company, do this first, because many permissions only work once MFA is on.
- **Never use the root account** and never ask for its password. Everything in this guide is done with your IAM user.
- Do not create access keys for yourself unless the company allows it. Use **AWS CloudShell** (next step). If you are given keys or SSO, keep them private: never put them in code, `.env`, git, email, or chat.

### Step 0.2. Use AWS CloudShell (a terminal inside the browser)

- Click the **terminal icon `>_`** at the top of the console, or open the **CloudShell** service. Choose the region `<REGION>` first.
- It is already signed in as you, so **no access keys are needed**. It is free to use. It is a Linux (bash) shell, so the commands in this guide run there.
- Check who you are:
  ```
  aws sts get-caller-identity
  aws configure get region
  ```
  The output must show your user and the company account. If the region is empty, run `aws configure set region <REGION>`.
- Upload files with **Actions → Upload file**. The shell keeps 1 GB of your files between sessions.
- To build the Docker image (Step 14) you need a computer with Docker, normally your laptop. CloudShell is x86 and cannot easily build the arm64 image Lambda uses here.

### Step 0.3. Find out what you are allowed to do

- Company accounts usually allow only part of what this guide does. You discover limits when you see **"is not authorized to perform ..."** or **"explicit deny in a service control policy"**.
- Quick tests in CloudShell (each should succeed or say AccessDenied):
  ```
  aws s3api list-buckets
  aws iam list-roles --max-items 3
  aws ec2 describe-instances --max-items 3
  aws ssm describe-instance-information --max-items 3
  aws scheduler list-schedules --max-items 3
  aws kms list-keys --max-items 3
  ```
- Anything denied goes on the **request list** below. Do not work around a denial. The restriction is deliberate and the administrator can grant it.

### Step 0.4. Send this request list to the AWS administrator

Ask them to give your user (or a role you can assume) these permissions, **restricted to resources whose names start with `hr-onboarding` or your bucket names** (that is what a least-privilege request looks like):

| Need | Permissions (service: actions) | Why |
|---|---|---|
| Storage | S3: create buckets (documents, restricted), bucket policy, encryption, versioning, lifecycle, **CORS**, logging, public-access-block, objects on those buckets | The data (Steps 4, 10, 18) |
| Shared bucket | S3 on `igtl-hr-analytics-ayush-2026`: read and write only under `employees/` and `employee_education/` for the two function roles `hr-onboarding-portal-role` and `hr-onboarding-hr-role`. This is the **bucket owner's** (your colleague's) permission, not yours. Part 4, Step 10j has the policy text | Steps 10j, 15 |
| Encryption | KMS: create key, alias `alias/hr-onboarding`, rotation, key policy | Steps 9, 12 |
| Database | DynamoDB: `CreateTable`, `UpdateContinuousBackups`, `UpdateTimeToLive`, `DescribeTable`, restore from point in time, on tables named `hr-onboarding-*` | Step 13 |
| Image | ECR: create repository `hr-onboarding`, push and pull images | Step 14 |
| Roles | IAM: `CreateRole`, `PutRolePolicy`, `PassRole` for roles named `hr-onboarding-*` only | Step 15 |
| Secrets | Systems Manager Parameter Store: put and read SecureString parameters under `/hr-onboarding/*`, using the KMS key | Step 16 |
| Functions | Lambda: create and update two functions named `hr-onboarding-*`, create Function URLs, `AddPermission`, `PutFunctionConcurrency`, `DeleteFunctionConcurrency`; Service Quotas: **view** the Lambda quotas (and ask for an increase if needed) | Steps 17, 19 |
| Front door | CloudFront: create two distributions, two CloudFront Functions, one origin access control, response headers policy; ACM certificates in **`us-east-1`**; Route 53 (or the company DNS team): two records for `<ONBOARDING_DOMAIN>` and `<HR_DOMAIN>` | Step 18 |
| Schedule | EventBridge Scheduler: create four schedules | Step 19 |
| Logs and alarms | CloudWatch Logs and Alarms; SNS create topic and subscribe | Part 3 |
| Audit | CloudTrail: create trail and event selectors (or, if the company has its own trail, ask them to add data events for your two data buckets and the three tables). **An administrator must create the role `CloudTrailRoleForCloudWatchLogs-hr-onboarding`** so the trail can send to CloudWatch Logs (Step 5) | Step 5 |
| Analytics | Glue and Athena: create database, tables, workgroup | Step 20 |
| Cost | Billing view and Budgets (an administrator must switch on "IAM user access to billing") | Step 2 |
| Optional | GuardDuty, Macie, Config, Backup | Part 7 |

Also ask the administrator these questions, and write down the answers:

- Is there already an **organization-wide CloudTrail**, **GuardDuty**, **Config**, **Security Hub**, or **budget**? If yes, skip your own copy and ask them to include your resources (this saves money and avoids duplicate alerts).
- Which **regions** are allowed? Is `ap-south-1` allowed?
- Is there a **naming or tagging rule** (for example every resource needs `CostCenter`)? Use it.
- May I use **Lambda, DynamoDB, CloudFront and ECR** in this account? CloudFront is a global service, and some company rules block it: check for a service control policy. What is the **Lambda concurrent-executions quota** (Step 17)?
- Who owns the two web addresses (`<ONBOARDING_DOMAIN>`, `<HR_DOMAIN>`), and can they add DNS records for the certificate and for CloudFront?
- Questions for your colleague (bucket `igtl-hr-analytics-ayush-2026`), see Step 10j: which account and region it is in, how it is encrypted, and whether Athena tables already exist on it.
- Who do I call if the portal is down or there is a security incident? (Fill the contact table in Part 9.)
- Who approves use of the **Gemini API** and storing Aadhaar and PAN (company policy and privacy sign-off)?

---

## Part 1. AWS words you need to know

- **Account**: the company's AWS "tenant". It holds everything and is billed monthly.
- **Region**: a physical location (Mumbai = `ap-south-1`). If something "disappeared", check the region name at the top-right of the console.
- **Console**: the website where you click. **CLI**: the `aws ...` commands. **CloudShell**: a terminal in the browser that already has the CLI.
- **IAM**: who may do what. A **user** is a person (you). A **role** is a set of permissions a service borrows. A **policy** is the written list of allowed actions.
- **S3**: file storage. A **bucket** is a top-level container. A **prefix** is a folder-like path (`onboarding/employees/IGTPL007/`). A **pre-signed link** is a web address that works for a few minutes and allows exactly one action on exactly one file.
- **DynamoDB**: a database where each record is an **item** in a **table**. **On demand** means you pay per request, nothing when idle. **Point-in-time recovery (PITR)** lets you rewind a table to any second in the last 35 days.
- **Lambda**: runs your code only when a request arrives. **Concurrency** is how many copies run at the same moment. **Reserved concurrency 0** means "do not run at all" (that is the night switch). A **cold start** is the extra second or two when a new copy starts after being idle.
- **Lambda Web Adapter**: a small helper inside the Docker image that lets an ordinary web app (Starlette/uvicorn) run on Lambda without changes.
- **Function URL**: a web address that Lambda gives a function. Here it is locked with IAM so only CloudFront can call it.
- **CloudFront**: the front door. It serves HTTPS, hides the Lambda address, and runs a tiny **CloudFront Function** on the HR address that lets only the office IP through. **OAC (origin access control)** is how CloudFront proves to Lambda that the request really came from it.
- **KMS**: holds the encryption key. DynamoDB and the secrets are unreadable without permission to use the key.
- **SSM Parameter Store**: stores secrets (the Gemini key, the HR login) as encrypted **SecureString** values. The app reads them once when a copy starts.
- **ECR**: stores the Docker image that Lambda runs.
- **EventBridge Scheduler**: runs an action at a set time. Here it switches the functions off at midnight and on at 7 am, India time.
- **Route 53**: DNS (domain names). **ACM**: free HTTPS certificates.
- **Glue / Athena**: Glue is the catalogue of your tables. Athena runs SQL on the files in S3.
- **CloudWatch**: logs, graphs, alarms. **CloudTrail**: records who did what in the account. **SNS**: sends the alarm email.

### Rules that prevent most AWS disasters

- Turn on **MFA** for your user.
- Never put an AWS key, password or API key in code, git, email, or chat. The functions use **roles**, not keys.
- Never make an S3 bucket public. HR data includes Aadhaar and PAN.
- Do not change settings you do not understand in a shared company account. Ask the administrator.

---

## Part 2. Every service you need, what it does here, and its price

**How to read:** each bullet is `Service: what it does here. Price (Mumbai, before 18 % GST). Required or optional.` "Free tier" means the first amount each month that AWS does not bill. **Lines marked "not re-checked" are planning estimates.**

### 2.1. Core services (you need all of these)

- **IAM**: your user and the three roles. **Free.** Required.
- **Lambda** (runs the two portals): **$0.20 per million requests and a small charge per GB-second; a monthly free tier covers 1 million requests and 400,000 GB-seconds** *(not re-checked)*. At 7 to 9 applications a day this is **about $0**. Required.
- **DynamoDB** (the records): on-demand, **about $1.25 per million writes and $0.25 per million reads; PITR about $0.20 per GB-month** *(not re-checked)*. A few hundred records are kilobytes, so **well under $1**. Required.
- **KMS** (encryption key): one key for DynamoDB and the secrets. **$1.00 per key per month; $0.03 per 10,000 requests; first 20,000 requests a month free.** Required.
- **S3** (storage): documents, HR tables, logs. **Storage $0.025 per GB-month (first 50 TB); PUT/COPY/POST/LIST $0.005 per 1,000 requests; GET $0.004 per 10,000 requests.** A few hundred employees is well under 1 GB. The shared bucket's storage is billed to your colleague. Required.
- **CloudFront** (HTTPS front door): **the always-free tier covers 1 TB of transfer and 10 million requests a month** *(not re-checked)*; a CloudFront Function costs **$0.10 per million invocations after 2 million free** *(not re-checked)*. About **$0**. Required.
- **ACM** (HTTPS certificates): **free.** Required.
- **ECR** (the image): **about $0.10 per GB-month** *(not re-checked)*. One image of about 0.5 GB is **cents**. Required.
- **SSM Parameter Store** (the secrets): standard parameters are **free** *(not re-checked)*. Required.
- **EventBridge Scheduler** (the night switch): **the first 14 million invocations a month are free** *(not re-checked)*. Required.
- **Route 53** (DNS for your two names): **$0.50 per hosted zone per month; $0.40 per million queries.** Optional: the company DNS team may add the two records instead.
- **Glue Data Catalog**: **free for the first 1 million objects and requests; then $1 per 100,000 objects per month and $1 per million requests.** Required.
- **Athena** (SQL on the HR tables): **$5.00 per TB scanned.** Your data is megabytes: cents. Required.
- **Gemini API** (reads the documents): **billed by Google, not by AWS.** Set a spending cap on the key. Required.

### 2.2. Logging, alerts and audit (set up first, Part 3)

- **CloudWatch Logs**: **$0.67 per GB ingested (Standard class); $0.03 per GB-month stored; Logs Insights queries $0.0067 per GB scanned.** A small app writes well under 1 GB a month. Required.
- **CloudWatch Alarms**: **$0.10 per standard alarm per month.** Ten alarms = $1.00 (AWS also has a monthly free tier for alarms; not re-checked). Required.
- **SNS** (alert emails): **first 1,000 email notifications a month free.** Required.
- **CloudTrail**: **your first copy of management events is free. S3 data events $0.000001 per event ($1.00 per million); DynamoDB data events are priced the same way** *(not re-checked)*. About 20,000 events a month = $0.02. Required (or the company's own trail).
- **S3 logs bucket**: the S3 storage price above, normally under $1 a month. Required.
- **AWS Budgets** (cost alerts): **first 2 budgets free** *(not re-checked)*. Required.

### 2.3. Security services (recommended; ask the administrator first, the company may already run them)

- **GuardDuty** (threat detection, including **S3 protection**): **S3 data events analyzed $0.000001 per event (first 500 million a month); CloudTrail events analyzed $0.0000044 per event.** A new account gets a free trial. About $1 to $5 a month at your volume.
- **Macie** (finds PAN and Aadhaar in the wrong place): **bucket inventory about $0.10 per bucket a month; object monitoring $0.0125 per 100,000 objects; sensitive data discovery $1.25 per GB scanned.** Scanning 1 GB once = $1.25. Run it weekly or monthly, not daily. Optional.
- **AWS Config**: **$0.003 per configuration item recorded, $0.001 per rule evaluation.** About $3 to $10 a month. Optional.
- **IAM Access Analyzer**: the basic external-access analyzer is **free** *(not re-checked)*. Recommended.
- **AWS Backup** (scheduled backups of S3): **$0.0385 per GB-month for S3 backup storage.** Optional (S3 versioning and DynamoDB PITR already cover most mistakes).
- **AWS WAF**: **not used in this design.** The HR address is limited to the office IP by a free CloudFront Function (Step 18), and a $5-plus-rules WAF would cost more than the whole rest of the bill. It stays an option if a client needs bot and rate protection (about **$5 per web ACL + $1 per rule a month + $0.60 per million requests**, *not re-checked for Mumbai*).

### 2.4. Services you do NOT need (so you do not pay for them)

- **A server (EC2), a load balancer (ALB), a NAT gateway, ECS/Fargate, autoscaling rules**: Lambda replaces them. The NAT gateway used to cost about $40 a month. It is no longer needed because the functions reach **DynamoDB, S3 and SSM through IAM over AWS's own network**, and **Gemini does not require a fixed IP address**, so nothing has to come from one known address.
- **MongoDB Atlas**: replaced by DynamoDB (see "Choosing the deployment option"). The functions have no fixed address, so Atlas would have needed a NAT gateway or an "allow all IPs" rule.
- **A VPC for the functions**: not needed. A function outside a VPC reaches S3, DynamoDB and the internet directly.
- **Secrets Manager**: SSM Parameter Store is free.
- **Interface VPC endpoints**: not needed at this size.
- **App Runner**: closed to new customers since 2026-04-30 (Appendix B).

---

## Part 3. Step by step: budget, alerts and logs FIRST

**Why logs first:** if something goes wrong later, logs are the only way to learn what happened. Logs that were not switched on beforehand cannot be recovered afterwards.

### Step 1. Check your sign-in (done in Part 0)

- Confirm in CloudShell: `aws sts get-caller-identity` shows your IAM user. Confirm MFA is on. **Cost: free.**

### Step 2. Set a budget alert

- Console → **Billing and Cost Management** → **Budgets** → **Create budget**. If it says you have no access, ask the administrator to enable **IAM user access to billing** or to create the budget for you.
- "Cost budget", monthly, fixed amount (suggest **$15** for this design; expected AWS cost is about $3 to $5 a month. The Gemini API is billed separately by Google). Alerts at **50 %**, **80 %**, **100 %** of actual cost and **100 % forecasted**. Enter your email and the administrator's.
- Optional: Billing → **Cost Anomaly Detection** → monitor "AWS services".
- **Cost: first 2 budgets free.** If an alert email arrives, go to Part 9, problem H.

### Step 3. Create the alert channel (SNS)

- Console → **SNS** → Topics → **Create topic** → type **Standard** → name `hr-onboarding-alerts`.
- **Create subscription** → protocol **Email** → your email. Add a second person's email too.
- Open the confirmation email and click **Confirm subscription**. **No alerts arrive until you confirm, and the link expires in about 3 days** (subscribe again if it does). The topic must be **Standard**, not FIFO.
  ```
  aws sns create-topic --name hr-onboarding-alerts
  aws sns subscribe --topic-arn arn:aws:sns:<REGION>:<ACCOUNT_ID>:hr-onboarding-alerts --protocol email --notification-endpoint you@example.com
  ```
- **Cost: first 1,000 emails a month free.**

### Step 4. Create the logs bucket

- This bucket stores CloudTrail files and S3 access logs. Nobody writes employee data here.
  ```
  aws s3api create-bucket --bucket <BUCKET_LOGS> --region <REGION> --create-bucket-configuration LocationConstraint=<REGION>
  aws s3api put-public-access-block --bucket <BUCKET_LOGS> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-versioning --bucket <BUCKET_LOGS> --versioning-configuration Status=Enabled
  aws s3api put-bucket-ownership-controls --bucket <BUCKET_LOGS> --ownership-controls '{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
  ```
- Encryption: leave the default (SSE-S3). **Do not use KMS on this bucket**, because the S3 log delivery service cannot write to a KMS bucket.
- Lifecycle: delete log files older than **365 days** (S3 → bucket → Management → Create lifecycle rule → Expire current versions after 365 days).
- Bucket policy: allow the logging services to write. Save as `logs-policy.json`, then `aws s3api put-bucket-policy --bucket <BUCKET_LOGS> --policy file://logs-policy.json`:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
       "Resource": ["arn:aws:s3:::<BUCKET_LOGS>", "arn:aws:s3:::<BUCKET_LOGS>/*"],
       "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
      {"Sid": "S3LogDelivery", "Effect": "Allow", "Principal": {"Service": "logging.s3.amazonaws.com"},
       "Action": "s3:PutObject", "Resource": "arn:aws:s3:::<BUCKET_LOGS>/s3-access-logs/*",
       "Condition": {"StringEquals": {"aws:SourceAccount": "<ACCOUNT_ID>"}}},
      {"Sid": "CloudTrailAclCheck", "Effect": "Allow", "Principal": {"Service": "cloudtrail.amazonaws.com"},
       "Action": "s3:GetBucketAcl", "Resource": "arn:aws:s3:::<BUCKET_LOGS>"},
      {"Sid": "CloudTrailWrite", "Effect": "Allow", "Principal": {"Service": "cloudtrail.amazonaws.com"},
       "Action": "s3:PutObject", "Resource": "arn:aws:s3:::<BUCKET_LOGS>/cloudtrail/AWSLogs/<ACCOUNT_ID>/*",
       "Condition": {"StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control"}}}
    ]
  }
  ```
- Folders that will appear inside: `cloudtrail/`, `s3-access-logs/main/`, `s3-access-logs/restricted/`.
- **Cost: $0.025 per GB-month. Expect under $1 a month.**

### Step 5. Turn on CloudTrail (the account's security camera)

- First ask the administrator: "Is there already an organization CloudTrail?" If **yes**, ask them to add **S3 data events** for `<BUCKET>` and `<BUCKET_RESTRICTED>` and **DynamoDB data events** for the three `hr-onboarding-*` tables (so you can see who opens each employee's file and record) and skip to Step 6.
- If you may create your own:
  - Console → **CloudTrail** → **Create trail**: name **`hr-onboarding-audit-trail`**, storage = existing bucket `<BUCKET_LOGS>` with prefix `cloudtrail`, **log file validation: Enabled**, **CloudWatch Logs: Enabled** (new log group `/hr-onboarding/cloudtrail`), **multi-region: Yes**, events: **Management events** read and write.
  ```
  aws cloudtrail create-trail --name hr-onboarding-audit-trail --s3-bucket-name <BUCKET_LOGS> --s3-key-prefix cloudtrail --is-multi-region-trail --enable-log-file-validation
  aws cloudtrail start-logging --name hr-onboarding-audit-trail
  ```
- **Important: the CloudWatch Logs part needs a role that an administrator must create.** Sending the trail to CloudWatch Logs needs an IAM role named **`CloudTrailRoleForCloudWatchLogs-hr-onboarding`** (trusted by `cloudtrail.amazonaws.com`, allowed `logs:CreateLogStream` and `logs:PutLogEvents` on `/hr-onboarding/cloudtrail`). A normal IAM user usually cannot create it. **Until it exists and is attached to the trail, the six security alarms in Step 7 and the "who opened a file" Logs Insights query in Step 8 do not work** (the trail still writes to the S3 logs bucket). Ask for it in the request list (Step 0.4). When you have it:
  ```
  aws cloudtrail update-trail --name hr-onboarding-audit-trail --cloud-watch-logs-log-group-arn arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/hr-onboarding/cloudtrail:* --cloud-watch-logs-role-arn arn:aws:iam::<ACCOUNT_ID>:role/CloudTrailRoleForCloudWatchLogs-hr-onboarding
  ```
- Add S3 data events once the data buckets exist (Step 10), and the DynamoDB events once the tables exist (Step 13; the combined command is in Part 7.1):
  ```
  aws cloudtrail put-event-selectors --trail-name hr-onboarding-audit-trail --advanced-event-selectors '[{"Name":"Management events","FieldSelectors":[{"Field":"eventCategory","Equals":["Management"]}]},{"Name":"S3 HR data","FieldSelectors":[{"Field":"eventCategory","Equals":["Data"]},{"Field":"resources.type","Equals":["AWS::S3::Object"]},{"Field":"resources.ARN","StartsWith":["arn:aws:s3:::<BUCKET>/","arn:aws:s3:::<BUCKET_RESTRICTED>/"]}]}]'
  ```
  This command replaces the trail's event settings, so it lists the management events too. In the console check that both "Management events" and "Data events" show as enabled.
- **Cost: first management trail free; data events $0.000001 each ($1 per million). Cents at your usage.**

### Step 6. Create the CloudWatch log groups (the app's diary)

- Each Lambda function writes to a log group named after it. Create them **now**, with a retention period, so they never grow forever and so the function (which is allowed only to write, not to create groups) can use them:
  ```
  aws logs create-log-group --log-group-name /aws/lambda/hr-onboarding-portal
  aws logs create-log-group --log-group-name /aws/lambda/hr-onboarding-hr
  aws logs put-retention-policy --log-group-name /aws/lambda/hr-onboarding-portal --retention-in-days 90
  aws logs put-retention-policy --log-group-name /aws/lambda/hr-onboarding-hr --retention-in-days 365
  ```
  (portal 90 days, HR 365 days: HR's log shows who opened which employee's documents.)
- **Always set a retention period.** Without it logs are kept forever and the bill grows.
- The app writes **JSON lines** with `levelname`, the module, a short message and the **employee ID only**. It never logs PAN, Aadhaar, account numbers, names, addresses or file content, and it logs an error as its type and place, not its text (`jsonlog.py`). Debug logging is **off** unless `LOG_LEVEL=DEBUG`.
- **Cost: $0.67 per GB ingested and $0.03 per GB-month stored. A small app writes well under 1 GB a month.**

### Step 7. Create the alarms (so you hear about problems)

**Three notes first (each one is a common reason an alarm never arrives):**
- The SNS topic `hr-onboarding-alerts` must be the **Standard** type, **not FIFO**. A FIFO topic cannot deliver email at all.
- The **email confirmation link expires in about 3 days**. If you did not click it in time, subscribe again (Step 3). No confirmed subscription, no alarm emails.
- In an alarm's **Topic ARN** field, paste the **full ARN** (`arn:aws:sns:<REGION>:<ACCOUNT_ID>:hr-onboarding-alerts`), not just the topic name.

**Security alarms** (these need the trail to be sending to CloudWatch Logs, which needs the administrator-created role from Step 5):
- CloudWatch → **Logs** → `/hr-onboarding/cloudtrail` → **Metric filters** → Create. For each row, create the filter, then **Create alarm** (threshold 1, period 5 minutes, action: send to `hr-onboarding-alerts`):

| Alarm name | Filter pattern (paste in the filter box) | What it means |
|---|---|---|
| failed-logins | `{ $.eventName = "ConsoleLogin" && $.errorMessage = "Failed authentication" }` | Wrong password attempts |
| login-no-mfa | `{ $.eventName = "ConsoleLogin" && $.additionalEventData.MFAUsed != "Yes" }` | A login without MFA |
| bucket-policy-change | `{ $.eventName = "PutBucketPolicy" || $.eventName = "DeleteBucketPolicy" || $.eventName = "PutBucketPublicAccessBlock" || $.eventName = "DeleteBucketPublicAccessBlock" }` | Someone changed bucket security |
| trail-tampering | `{ $.eventName = "StopLogging" || $.eventName = "DeleteTrail" || $.eventName = "UpdateTrail" }` | Someone tried to turn off logs |
| iam-change | `{ $.eventName = "PutUserPolicy" || $.eventName = "PutRolePolicy" || $.eventName = "AttachRolePolicy" || $.eventName = "CreateAccessKey" }` | Permissions changed or key created |
| kms-disable | `{ $.eventName = "DisableKey" || $.eventName = "ScheduleKeyDeletion" }` | Encryption key at risk |

**App alarms** (after Step 17, when the functions exist):
- **`portal-errors`**: a metric filter on the **Lambda log group `/aws/lambda/hr-onboarding-portal`** with the pattern `{ $.levelname = "ERROR" }` and an alarm at **5 matches in 5 minutes**. The app logs a failed table write and keeps the submission, so this alarm tells you to run `python scripts/sync_tables.py`. Make a second one for `/aws/lambda/hr-onboarding-hr`.
  - **A metric filter can only find the word ERROR if the level is printed in every log line.** In a plain Python log format that means **`%(levelname)s`** must be part of the format. The app's JSON lines already carry a **`levelname`** field, which is what the pattern above matches. If you ever see an alarm that never fires, open one log line and check that the level is there.
- **`lambda-errors`**: CloudWatch → Alarms → Create alarm → Select metric → **Lambda → By Function Name → `Errors`** for each function, `Sum` ≥ 1 over 5 minutes. Set **treat missing data as "not breaching"** (the functions are idle at night).
- **Do not alarm on `Throttles`.** The night switch makes every request a throttle on purpose (Step 19).
- Optional: **`Duration`** p99 above 45 seconds (a function close to its 60-second limit).
- **Cost: $0.10 per alarm per month.**

### Step 8. Learn to read logs now (5-minute practice)

- **Who did what in AWS:** Console → **CloudTrail** → **Event history** → filter by "User name", "Event name" (for example `DeleteObject`), or "Resource name". Shows the last 90 days of management events, free.
- **Why the app failed:** CloudWatch → Logs → Log groups → `/aws/lambda/hr-onboarding-portal` → open the newest **log stream** → Ctrl+F and search `ERROR` or the employee ID. The `REPORT` line at the end of each request shows `Duration`, `Billed Duration` and `Max Memory Used`.
- **Search many lines at once:** CloudWatch → **Logs Insights** → select the log group → run (the fields exist because the app writes JSON):
  ```
  fields @timestamp, levelname, employee_id, message, error_type, where
  | filter levelname = "ERROR"
  | sort @timestamp desc
  | limit 50
  ```
- **Everything that happened to one employee in the app:**
  ```
  fields @timestamp, levelname, message
  | filter employee_id = "IGTPL007"
  | sort @timestamp asc
  ```
- **Who opened a specific employee's file or record** (needs data events and the CloudWatch role from Step 5), Logs Insights on `/hr-onboarding/cloudtrail`:
  ```
  fields @timestamp, userIdentity.arn, eventName, requestParameters.key
  | filter eventName = "GetObject" and requestParameters.key like /IGTPL007/
  | sort @timestamp desc
  ```
  For DynamoDB, filter `eventSource = "dynamodb.amazonaws.com"` and `requestParameters.key.employee_id.s = "IGTPL007"`.
- **Web requests:** the app's own access log is **off on purpose**, because the employee edit link carries a secret token in the URL. CloudFront standard logs are **off too** (they would record query strings); if you ever turn them on, send them to `<BUCKET_LOGS>` and restrict that bucket.
- **Detailed S3 access:** S3 server access logs in `s3://<BUCKET_LOGS>/s3-access-logs/`.
- Keep a note of these places. Part 9 refers back to them.

---
## Part 4. Step by step: encryption and storage (S3)

### Step 9. Create the encryption key (KMS)

- Console → **KMS** → Customer managed keys → **Create key**: **Symmetric**, **Encrypt and decrypt**, alias `hr-onboarding`, description "HR onboarding data". Key administrators: your IAM user. Key users: leave empty (the two function roles get the key through their own policies in Step 15).
- Open the key → **Key rotation** → enable automatic rotation.
  ```
  aws kms create-key --description "HR onboarding data"
  aws kms create-alias --alias-name alias/hr-onboarding --target-key-id <KEY_ID>
  aws kms enable-key-rotation --key-id <KEY_ID>
  ```
- Copy the **ARN** from the key page. That is `<KMS_KEY_ARN>`. It encrypts the **three DynamoDB tables** (Step 13) and the **SSM secrets** (Step 16), so it is **required**. It is **not** the app's `S3_KMS_KEY_ID`: leave that empty (Step 10c).
- **Warning:** if this key is deleted, the DynamoDB tables and the secrets become permanently unreadable. Never schedule deletion. The alarm in Step 7 watches for it.
- **Cost: $1.00 a month + $0.03 per 10,000 requests.**

### Step 10. Create the two data buckets

- Create **your own two** private buckets: `<BUCKET>` (the students' uploaded documents) and `<BUCKET_RESTRICTED>` (bank and sensitive tables only). The shared bucket `igtl-hr-analytics-ayush-2026` already exists and is **not** created or changed by you (see Step 10j):
  ```
  aws s3api create-bucket --bucket <BUCKET> --region <REGION> --create-bucket-configuration LocationConstraint=<REGION>
  aws s3api create-bucket --bucket <BUCKET_RESTRICTED> --region <REGION> --create-bucket-configuration LocationConstraint=<REGION>
  ```
- For **each** of the two buckets (replace `<NAME>`), apply every setting below. If you cannot change account-level S3 settings, that is fine; bucket-level settings are enough.

**10a. Block public access** (all four options on)
```
aws s3api put-public-access-block --bucket <NAME> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

**10b. Versioning on.** Lets you recover a deleted or overwritten file (Part 9, problem F).
```
aws s3api put-bucket-versioning --bucket <NAME> --versioning-configuration Status=Enabled
```

**10c. Default encryption.** Important: the app has **one** setting, `S3_KMS_KEY_ID`, and it applies to **all three** buckets it writes, including the shared one. If you set your own key, the HR tables you write into your colleague's bucket are encrypted with **your** key and their Athena cannot read them. So:
- **Recommended: leave `S3_KMS_KEY_ID` empty.** The app then writes with S3's own encryption (SSE-S3, AES-256) everywhere. Skip the command below (new buckets already default to SSE-S3). Your own KMS key still protects DynamoDB and the secrets. **The browser's upload to S3 forces SSE-S3 (`AES256`), so keep the buckets on SSE-S3.**
- Only if the company requires a KMS key on all S3 data: ask your colleague for the KMS key their bucket uses, set `S3_KMS_KEY_ID` to it, use the same key in the command below and in Step 15 (the pre-signed upload then enforces KMS instead of AES256).
```
aws s3api put-bucket-encryption --bucket <NAME> --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<KMS_KEY_ARN>"},"BucketKeyEnabled":true}]}'
```

**10d. Turn off ACLs**
```
aws s3api put-bucket-ownership-controls --bucket <NAME> --ownership-controls '{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
```

**10e. HTTPS-only bucket policy** (save as `policy.json`, then `aws s3api put-bucket-policy --bucket <NAME> --policy file://policy.json`):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::<NAME>", "arn:aws:s3:::<NAME>/*"],
     "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
    {"Sid": "DenyOldTLS", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::<NAME>", "arn:aws:s3:::<NAME>/*"],
     "Condition": {"NumericLessThan": {"s3:TlsVersion": "1.2"}}}
  ]
}
```

**10f. Lifecycle (documents bucket only).** Save as `lifecycle.json`, then `aws s3api put-bucket-lifecycle-configuration --bucket <BUCKET> --lifecycle-configuration file://lifecycle.json`:
```json
{"Rules": [
  {"ID": "pending-uploads-1-day", "Status": "Enabled", "Filter": {"Prefix": "onboarding/pending/"},
   "Expiration": {"Days": 1},
   "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
  {"ID": "pending-delete-markers", "Status": "Enabled", "Filter": {"Prefix": "onboarding/pending/"},
   "Expiration": {"ExpiredObjectDeleteMarker": true}},
  {"ID": "old-versions", "Status": "Enabled", "Filter": {"Prefix": "onboarding/employees/"},
   "NoncurrentVersionTransitions": [{"NoncurrentDays": 30, "StorageClass": "STANDARD_IA"}],
   "NoncurrentVersionExpiration": {"NoncurrentDays": 365},
   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
  {"ID": "athena-results", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"},
   "Expiration": {"Days": 30}}
]}
```
- **The first rule is what cleans up the waiting folder.** The browser uploads each file to `onboarding/pending/<random id>/` before any employee ID exists; at submit the app copies the file into `onboarding/employees/<ID>/documents/`. Nobody (no role) has `DeleteObject`, so S3 itself removes the waiting copies after **1 day** (and their old versions, because the bucket is versioned). An abandoned form leaves nothing behind after a day and has used no ID.
- Do not add an expiry rule on `onboarding/employees/` unless your company retention policy says how long to keep records.

**10g. Access logging** (records every access in the logs bucket)
```
aws s3api put-bucket-logging --bucket <BUCKET> --bucket-logging-status '{"LoggingEnabled":{"TargetBucket":"<BUCKET_LOGS>","TargetPrefix":"s3-access-logs/main/"}}'
aws s3api put-bucket-logging --bucket <BUCKET_RESTRICTED> --bucket-logging-status '{"LoggingEnabled":{"TargetBucket":"<BUCKET_LOGS>","TargetPrefix":"s3-access-logs/restricted/"}}'
```

**10h. Go back to Step 5** and add the CloudTrail S3 data events for these two buckets.

**10i. (Moved.)** The "only the two roles and named administrators may read the private buckets" deny rule is now Step 21, so you can apply it last, after everything works, and test it on a throwaway bucket first.

**10j. The shared bucket `igtl-hr-analytics-ayush-2026` (your colleague's).** You do not create or configure it. Ask your colleague these four things and write down the answers:

1. **Which AWS account owns it?** Same account as yours: only the roles' own policies (Step 15) are needed. A **different** account: they must also add the bucket policy below.
2. **Which region?** `aws s3api get-bucket-location --bucket igtl-hr-analytics-ayush-2026`. Use that as `<REGION>` for everything.
3. **How is it encrypted?** SSE-S3 works as it is (leave `S3_KMS_KEY_ID` empty). If it uses their own KMS key, they must allow your two function roles in that key's policy, and you set `S3_KMS_KEY_ID` to it.
4. **Is "Bucket owner enforced" on** (the S3 default)? Then the files you write belong to them and their Athena can read them. Also ask: **do the Athena tables from `samples/hr_tables/athena_tables.sql` already exist on this bucket?** (Step 20 depends on it.)

If the bucket is in a **different account**, send them this to add to the bucket policy (fill in `YOUR_ACCOUNT_ID`), plus a second statement allowing `s3:ListBucket` on `arn:aws:s3:::igtl-hr-analytics-ayush-2026` for the same role:
```json
{"Sid": "HROnboardingApp", "Effect": "Allow",
 "Principal": {"AWS": ["arn:aws:iam::YOUR_ACCOUNT_ID:role/hr-onboarding-portal-role",
                       "arn:aws:iam::YOUR_ACCOUNT_ID:role/hr-onboarding-hr-role"]},
 "Action": ["s3:GetObject", "s3:PutObject"],
 "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
              "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*"]}
```
The live Athena tables (database `hr_analytics`) in this bucket point at top-level folders, with **no `hr/` prefix**: `s3://igtl-hr-analytics-ayush-2026/employees/` and `.../employee_education/`. The app writes there by default (`SHARED_TABLE_PREFIX` is empty; set it to `hr/` only if you ever need the old layout).

What the app does in that bucket: it **only writes its own files**, `employees/IGTPL###.parquet` and `employee_education/IGTPL###.parquet`. It never lists, overwrites or deletes your colleague's files, and it has **no delete permission** there. **Do not** put the ID documents or the bank and sensitive tables in the shared bucket: everyone who can read it or query its Athena tables would see real PAN, Aadhaar and account numbers. In the `employees` table your Athena users will see your real students next to any sample rows (filter with `employee_id LIKE 'IGTPL%'`).

- **Cost:** storage $0.025 per GB-month, PUT $0.005 per 1,000, GET $0.004 per 10,000. A small HR dataset is about **$1 to $2 a month**.

### Step 11. What the data looks like (one folder per employee, DynamoDB + S3)

This is written by the code (`storage.py`, `hr_tables.py`). Do not change the key names. The employee ID (for example `IGTPL007`) comes from an **atomic counter in DynamoDB**, so two people can never get the same ID. It is the item key in DynamoDB and the name of the employee's S3 folder.

- **DynamoDB** (the three tables from Step 13)
  - `hr-onboarding-applications`: one item per employee, `employee_id = IGTPL007`: the whole record (personal, family, education, bank, references, policies, `application_status`, `flags`, and `document_folder`, the S3 address of the employee's folder). An ID is claimed only when the application is submitted: for an instant (or after a failed submit) the item is a short-lived `Draft` that HR never sees.
  - `hr-onboarding-forms`: the form as submitted, so a flagged form can be reopened.
  - `hr-onboarding-counters`: the ID counter.
- **Your private documents bucket `<BUCKET>`**
  - `onboarding/employees/IGTPL007/documents/` is that employee's own folder: `pan.jpg`, `aadhaar_front.jpg`, `aadhaar_back.jpg`, `photo.jpg`, `resume.pdf`, `education_certificate.pdf`, `bank_proof.jpg`, `payslip_N.pdf`, `relieving_letter.pdf`. **The browser first puts each file in a waiting folder**, `onboarding/pending/<random id>/<name>`, with a pre-signed link that names exactly one key, works for 5 minutes, allows 1 byte to 5 MB, only the right type (`image/jpeg` or `application/pdf`), and forces SSE-S3 encryption. **When the application is submitted** the files are checked, the ID is claimed and each file is **copied** into the employee's folder. A lifecycle rule removes everything under `pending/` after one day.
  - `athena-results/` holds Athena query output (auto-deleted after 30 days).
- **The shared bucket `igtl-hr-analytics-ayush-2026`** (your colleague's, you only add files)
  - `employees/IGTPL007.parquet` and `employee_education/IGTPL007.parquet` (top level, no `hr/` prefix): one small file per employee per table, same columns as `samples/hr_tables/athena_tables.sql`. Written when the employee submits, updated when HR approves, rejects or flags. Columns that HR fills in later (`department`, `work_email`, `manager_id` ...) are never overwritten by the app. These are the tables HR and Athena read.
- **Your restricted bucket `<BUCKET_RESTRICTED>`**
  - `hr/employee_bank_details/IGTPL007.parquet`
  - `hr/employee_sensitive_details/IGTPL007.parquet` (PAN, Aadhaar, phone, account numbers)
- **Not filled by the app:** `employee_compensation`, `employment_details`, `leave_records`, `attendance` (the form does not collect them).
- **Logs bucket `<BUCKET_LOGS>`**: `cloudtrail/`, `s3-access-logs/`.
- **How HR fetches:** the HR portal reads the records from DynamoDB and opens the documents through a 5-minute link, so it always shows the latest data. For reports, HR runs Athena on the tables in the shared bucket.
- **The submit is "all or nothing" for HR, and an unfinished form uses no ID:** nothing is claimed while the employee is still uploading. At submit the app first checks the form and every waiting file in S3 (it exists, it is not empty, it is within the size limit, and its first bytes match its type). Only then is the ID claimed, the files copied and the record saved. If a check fails, the employee sees a clear message and **no ID has been used**; the same waiting folder is kept for the retry. If the copy fails after the ID was claimed, HR sees nothing and the retry gets the next number (a gap, never a duplicate).
- **Why one folder per employee:** you can grant, audit, export, or delete one person's whole record using the ID (needed for a data-deletion request: Part 7.2).
- Keep names, PAN, and Aadhaar **out of file names and folder names**. Use the employee ID only.

---

## Choosing the deployment option

This guide builds **Option A**. The table shows what you give up or gain with the other three, so you can answer a client who asks "why not ...?". Costs are **approximate monthly AWS costs in Mumbai, before 18 % GST**, for about 7 to 9 applications a day. They are planning figures: **confirm them in the AWS Pricing Calculator** (https://calculator.aws).

| Option | Cost/month | Scaling | Night off | Upkeep | Privacy |
|---|---|---|---|---|---|
| **A. Lambda + DynamoDB + S3 (CHOSEN)** | ~$3-5 | automatic | EventBridge Scheduler | very low | highest |
| B. One EC2 t4g.small (Docker Compose + Caddy HTTPS), DynamoDB | ~$12-18 | manual / limited | Instance Scheduler | medium (OS patching) | good |
| C. ECS Fargate + ALB, DynamoDB, public subnets | ~$50-60 | automatic | scheduled scaling | low | high |
| D. Original: ECS + ALB + NAT gateway + MongoDB Atlas | ~$91 + Atlas | automatic | scheduled scaling | low | high |

**Why A:** it costs the least, nothing runs (or bills) when nobody is using the app, it adds copies by itself, there is no operating system to patch, and every component is reached through IAM, which gives the tightest privacy story. The price you pay: a request is limited to 6 MB (so each document goes to S3 directly), the first request after an idle period can take a second or two longer (cold start), and a request can last at most 15 minutes (the app uses 60 seconds).

**Why MongoDB was replaced.** MongoDB Atlas lives outside AWS and only accepts connections from addresses on a list. A Lambda function has **no fixed address**, so Atlas would need either **a NAT gateway with an Elastic IP** (about $40 a month, more than the entire rest of Option A) or **an allow-list of all IP addresses** (which removes the main protection on a database full of Aadhaar and PAN). DynamoDB is reached with the function's IAM role, so there is **no password, no address list and no NAT**. It is also on demand (no idle cost), encrypted with your KMS key, rewindable for 35 days with PITR, and AWS runs it.

**Why NAT is no longer needed.** NAT gives private containers one fixed outbound address. Here (1) DynamoDB, S3 and SSM are reached through IAM, not an address list, and (2) the only outside service, **Gemini, needs no fixed IP** (it uses the API key). So the functions run outside any VPC and have no NAT at all.

**If you ever need B, C or D** (a client that wants a fixed server, or fixed container capacity): they are not built in this guide. The EC2 + MongoDB version was removed from the project (git history, commit `18bb9f3`: `archive/`, `Caddyfile`, `docker-compose.aws.yml`, `DEPLOY_AWS.md`), and the code still runs in a container anywhere (`docker run` with `STORAGE_BACKEND=dynamodb_s3`, or `local` for a laptop; `docker-compose.yml` starts both portals on one machine).

---

## Part 5. Step by step: tables, image, roles, settings

### Step 12. The encryption key (Step 9) is required now

- In the earlier plan the KMS key was optional. **Now it is required**: **DynamoDB** encrypts its three tables with it (Step 13) and **SSM** encrypts the secrets with it (Step 16). If you skipped Step 9, do it first.
- The key's default policy already lets IAM policies grant access, so you do **not** edit the key policy. You grant each role the key in Step 15.
- Check the key and its rotation:
  ```
  aws kms describe-key --key-id alias/hr-onboarding --query "KeyMetadata.[KeyState,KeyManager]"
  aws kms get-key-rotation-status --key-id alias/hr-onboarding
  ```
  Expect `Enabled` and `CUSTOMER`, and rotation `true`.
- The buckets stay on **SSE-S3** (Step 10c), not on this key, and `S3_KMS_KEY_ID` stays **empty**. The shared bucket is your colleague's, and their Athena can only read files written with the bucket's own encryption.
- **Cost:** $1.00 a month (already counted in Step 9).

### Step 13. DynamoDB tables

Three tables, **on-demand billing, encrypted with your KMS key, point-in-time recovery on, deletion protection on**. The names are settings (`DDB_APPLICATIONS_TABLE`, `DDB_FORMS_TABLE`, `DDB_COUNTERS_TABLE`); these are the defaults.

| Table | Key | Holds |
|---|---|---|
| `hr-onboarding-applications` | `employee_id` (string) | The whole record: `application_status`, `flags`, `document_folder`, personal, family, education, bank, policies. An ID is claimed only when an application is submitted; the claim is a short-lived item with status `Draft` that the real record replaces a moment later |
| `hr-onboarding-forms` | `employee_id` (string) | The form as submitted, so a flagged form can be reopened |
| `hr-onboarding-counters` | `name` (string) | Two kinds of item. **`employee_id`**: the number `seq`, which the app adds 1 to with an atomic update to get the next `IGTPL###`. **`login#<user name>`**: the HR sign-in lock-out count (`attempts`, and `expires_at` 5 minutes after the first wrong password), so five wrong passwords lock that user out in **every** copy of the HR function |

```
for T in applications forms; do
  aws dynamodb create-table --table-name hr-onboarding-$T \
    --attribute-definitions AttributeName=employee_id,AttributeType=S \
    --key-schema AttributeName=employee_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --sse-specification Enabled=true,SSEType=KMS,KMSMasterKeyId=<KMS_KEY_ARN> \
    --deletion-protection-enabled --region <REGION>
done
aws dynamodb create-table --table-name hr-onboarding-counters \
  --attribute-definitions AttributeName=name,AttributeType=S \
  --key-schema AttributeName=name,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --sse-specification Enabled=true,SSEType=KMS,KMSMasterKeyId=<KMS_KEY_ARN> \
  --deletion-protection-enabled --region <REGION>
```

Wait until the three tables are `ACTIVE`, then:

```
for T in hr-onboarding-applications hr-onboarding-forms hr-onboarding-counters; do
  aws dynamodb update-continuous-backups --table-name $T \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true
done
aws dynamodb update-time-to-live --table-name hr-onboarding-applications \
  --time-to-live-specification Enabled=true,AttributeName=expires_at
aws dynamodb update-time-to-live --table-name hr-onboarding-counters \
  --time-to-live-specification Enabled=true,AttributeName=expires_at
```

- **What the time-to-live line does:** the claim of an ID (the `Draft` item) has an `expires_at` seven days ahead. If a submit fails half way, DynamoDB removes that leftover claim later (the number is not reused; a gap in the IDs is the only trace). A real, submitted record has **no** `expires_at`, so it is never removed. **The counters table has the same TTL attribute** so the short-lived `login#...` lock-out items disappear by themselves; the `employee_id` counter has no `expires_at`, so it stays. (TTL deletion can lag by hours, so the app also treats an item past its `expires_at` as zero.)
- **Check:** `aws dynamodb describe-table --table-name hr-onboarding-applications --query "Table.[TableStatus,SSEDescription.Status,DeletionProtectionEnabled]"` shows `ACTIVE`, `ENABLED`, `true`.
- **Nothing to create by hand later.** The app only reads and writes items.
- **Cost:** on-demand, a few cents at your volume; PITR about $0.20 per GB-month (kilobytes of data). *(Not re-checked.)*

### Step 14. The image: ECR and an arm64 build with the Lambda Web Adapter

The `Dockerfile` already contains the Lambda Web Adapter. Lambda runs arm64 (Graviton), which is cheaper, so the image is built for arm64.

- Create the repository (once), with scanning, encryption and tags that cannot be overwritten:
  ```
  aws ecr create-repository --repository-name hr-onboarding --image-tag-mutability IMMUTABLE \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=KMS,kmsKey=<KMS_KEY_ARN> --region <REGION>
  ```
- Build and push **from a computer that has Docker** (your laptop with Docker Desktop is fine; CloudShell is x86, so use the laptop or AWS CodeBuild). Sign in to AWS on that computer through the company's way (SSO or a CLI profile), **never with keys written into files in the project**:
  ```
  aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
  docker buildx build --platform linux/arm64 --provenance=false -t <ECR_URI>:v1 --push .
  ```
  - `--platform linux/arm64`: Lambda is configured for arm64 in Step 17. A wrong-CPU image fails with `exec format error`.
  - `--provenance=false`: Lambda does not accept the extra manifest that Docker adds by default ("image manifest ... is not supported").
  - Use a **new tag for every release** (`v1`, `v2`...). Tags cannot be reused, which is what lets you roll back (problem L).
  - **The build has two stages.** The pages are written in JSX; the strict Content-Security-Policy (Step 18) forbids compiling it in the browser, so the first stage compiles it with Babel (running in V8 through the `mini-racer` package) into plain files in `static/dist/`, and the second stage, the image that runs, only copies those files. The first stage runs on the machine that builds (it is plain text, the same for every CPU), so `mini-racer` is not in the Lambda image. If you change a page and run the app on a laptop, `python scripts/build_assets.py` (or just starting the app, when `mini-racer` is installed) rebuilds them.
  - **Base images are pinned** by tag **and** digest in the `Dockerfile` (`python:3.12.16-slim@sha256:...`, `aws-lambda-adapter:0.9.1@sha256:...`), so a rebuild cannot pick up a different image. To move to a newer one: `docker buildx imagetools inspect python:3.12-slim` shows the current tag and digest; build with `--build-arg PYTHON_IMAGE=python:3.12.<patch>-slim@sha256:<digest>` (and `--build-arg ADAPTER_IMAGE=...` for the adapter). Do this every quarter (Part 9 routine checks) to receive Python security fixes.
- Keep only the last five images:
  ```
  aws ecr put-lifecycle-policy --repository-name hr-onboarding --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep 5","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}'
  ```
- The image never contains `.env`, keys or passwords (`.dockerignore` and the `Dockerfile` leave them out).
- **Cost:** about $0.10 per GB-month. *(Not re-checked.)*

### Step 15. IAM roles (least privilege, one per function)

Three roles. Replace `<ACCOUNT_ID>`, `<REGION>`, and the bucket names. **No role can delete anything in S3 or DynamoDB, and the portal role cannot Scan.**

Trust policy for the two function roles (save as `trust-lambda.json`) and for the scheduler role (`trust-scheduler.json`, same text with `scheduler.amazonaws.com`):
```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
```
```
aws iam create-role --role-name hr-onboarding-portal-role --assume-role-policy-document file://trust-lambda.json
aws iam create-role --role-name hr-onboarding-hr-role --assume-role-policy-document file://trust-lambda.json
aws iam create-role --role-name hr-onboarding-scheduler-role --assume-role-policy-document file://trust-scheduler.json
```

**15a. `hr-onboarding-portal-role`** (the employee portal). Save as `portal-policy.json`, then `aws iam put-role-policy --role-name hr-onboarding-portal-role --policy-name portal --policy-document file://portal-policy.json`:
```json
{"Version": "2012-10-17", "Statement": [
  {"Sid": "Tables", "Effect": "Allow",
   "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"],
   "Resource": ["arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-applications",
                "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-forms"]},
  {"Sid": "IdCounterOnly", "Effect": "Allow",
   "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
   "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-counters",
   "Condition": {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["employee_id"]}}},
  {"Sid": "PendingUploads", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
   "Resource": "arn:aws:s3:::igtpl-hr-onboarding-docs-2026/onboarding/pending/*"},
  {"Sid": "EmployeeDocuments", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
   "Resource": "arn:aws:s3:::igtpl-hr-onboarding-docs-2026/onboarding/employees/*"},
  {"Sid": "RestrictedTables", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
   "Resource": ["arn:aws:s3:::igtpl-hr-onboarding-restricted-2026/hr/employee_bank_details/*",
                "arn:aws:s3:::igtpl-hr-onboarding-restricted-2026/hr/employee_sensitive_details/*"]},
  {"Sid": "SharedTablesNoDelete", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
   "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
                "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*"]},
  {"Sid": "SharedListOnlyOwnFolders", "Effect": "Allow", "Action": "s3:ListBucket",
   "Resource": "arn:aws:s3:::igtl-hr-analytics-ayush-2026",
   "Condition": {"StringLike": {"s3:prefix": ["employees/*", "employee_education/*"]}}},
  {"Sid": "Key", "Effect": "Allow",
   "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
   "Resource": "<KMS_KEY_ARN>"},
  {"Sid": "Secrets", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
   "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/hr-onboarding/portal/*"},
  {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
   "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/lambda/hr-onboarding-portal:*"}
]}
```
- **Why the portal role has these S3 permissions on the documents bucket:** the browser's pre-signed upload is authorised as **this** role, so `PutObject` on `onboarding/pending/*` is what lets an employee upload. At submit the app checks each waiting file (`HeadObject` and a read of its first 8 bytes: `GetObject` on `pending/*`), then copies it into the employee's folder with `CopyObject` (`GetObject` on the source, `PutObject` on `onboarding/employees/*`). `GetObject` on `employees/*` also lets an employee see their own documents on their edit link. The same read-before-rewrite need is why the table folders also have `GetObject`: the app reads a student's existing row so the columns HR fills in later are never overwritten.
- **No `s3:DeleteObject` anywhere, no `dynamodb:Scan`, no `dynamodb:DeleteItem`, no `s3:ListBucket` on the documents bucket.** The portal can add and fix, never remove or list. The waiting copies under `pending/` are removed by an S3 lifecycle rule after one day (Step 10f), not by any role. (A document an employee removed while fixing a flagged application stays in S3, unused; see Part 7.2.)

**15b. `hr-onboarding-hr-role`** (the HR portal). Save as `hr-policy.json`, then `aws iam put-role-policy --role-name hr-onboarding-hr-role --policy-name hr --policy-document file://hr-policy.json`:
```json
{"Version": "2012-10-17", "Statement": [
  {"Sid": "ApplicationsReadUpdate", "Effect": "Allow",
   "Action": ["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"],
   "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-applications"},
  {"Sid": "LoginLockoutOnly", "Effect": "Allow",
   "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
   "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-counters",
   "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["login#*"]}}},
  {"Sid": "FormsRead", "Effect": "Allow", "Action": ["dynamodb:GetItem"],
   "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-forms"},
  {"Sid": "DocumentsRead", "Effect": "Allow", "Action": "s3:GetObject",
   "Resource": "arn:aws:s3:::igtpl-hr-onboarding-docs-2026/onboarding/employees/*"},
  {"Sid": "DocumentsList", "Effect": "Allow", "Action": "s3:ListBucket",
   "Resource": "arn:aws:s3:::igtpl-hr-onboarding-docs-2026",
   "Condition": {"StringLike": {"s3:prefix": "onboarding/employees/*"}}},
  {"Sid": "RestrictedTables", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
   "Resource": ["arn:aws:s3:::igtpl-hr-onboarding-restricted-2026/hr/employee_bank_details/*",
                "arn:aws:s3:::igtpl-hr-onboarding-restricted-2026/hr/employee_sensitive_details/*"]},
  {"Sid": "SharedTablesNoDelete", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
   "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
                "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*"]},
  {"Sid": "Key", "Effect": "Allow",
   "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
   "Resource": "<KMS_KEY_ARN>"},
  {"Sid": "Secrets", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
   "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/hr-onboarding/hr/*"},
  {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
   "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/lambda/hr-onboarding-hr:*"}
]}
```
- **`dynamodb:Scan` is only on the HR role.** The HR list needs every record. The portal role has no `Scan`.
- **The counters table is split by key.** The sign-in lock-out (`login#<user>` items) is kept in `hr-onboarding-counters`, so the HR role may `GetItem` and `UpdateItem` **only on keys that start with `login#`** (the `dynamodb:LeadingKeys` condition), and never `DeleteItem` (a good sign-in sets the count to 0 instead). The one-time-code step that was last used (`last_step`) is stored on the same `login#<user>` item, which is how a code is refused the second time. It cannot touch the `employee_id` counter. In the same way the portal role may use that table only for the key `employee_id`. If your administrator cannot use `LeadingKeys` conditions, the fallback is a **fourth table** `hr-onboarding-logins` (key `name`, TTL `expires_at`, setting `DDB_COUNTERS_TABLE` unchanged and a code change to point the lock-out at it): ask before relying on it.
- **`PutItem`/`UpdateItem` on the applications table:** HR changes a status or adds a flag by saving the record again. The save is refused if the record does not exist (it cannot create a new employee).
- **`s3:PutObject` on the restricted and shared table folders:** when HR approves or flags, the app rewrites that student's table rows (`hr_tables.py`). There is no permission to delete, and none on the documents bucket.
- The download of a document is a **pre-signed link made by this role**, valid 5 minutes, only after the HR login.

**15c. `hr-onboarding-scheduler-role`** (the night switch, Step 19). It may only set or remove the concurrency limit on **these two functions**. Save as `scheduler-policy.json`, then `aws iam put-role-policy --role-name hr-onboarding-scheduler-role --policy-name night-switch --policy-document file://scheduler-policy.json`:
```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency"],
   "Resource": ["arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:hr-onboarding-portal",
                "arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:hr-onboarding-hr"]}
]}
```
- **Ask the administrator** to create these (or to let you create roles named `hr-onboarding-*` only), and to add `iam:PassRole` for your user on `hr-onboarding-scheduler-role` and the two function roles.
- **Cost: free.**

### Step 16. Settings and secrets in SSM Parameter Store

Secrets are **SecureString** values encrypted with your KMS key. The app reads them **once when a function copy starts** (cached while it stays warm, so a normal request makes no SSM call).

| Parameter name | Read by | Holds |
|---|---|---|
| `/hr-onboarding/portal/GEMINI_API_KEY` | portal | The Gemini key |
| `/hr-onboarding/hr/HR_USERS` | HR | A **JSON map** of HR login name → `{"hash": password hash, "totp": authenticator secret}`, for example `{"priya": {"hash": "scrypt$...", "totp": "JBSWY3DP..."}}`: one person, one login, one phone. Made by the script below; it never holds a password. **A SecureString, so the authenticator secrets are encrypted with the KMS key.** A standard SSM parameter holds 4 KB, which is about 12 users with this layout |
| `/hr-onboarding/hr/SESSION_SECRET` | HR | A random secret (32 or more characters) that signs the HR sign-in cookie |

- **Make or change the users on your own computer** (nothing is sent anywhere). Each HR person gets their own **login name** (letters, digits, dot, dash, underscore: a login name such as `priya` or `hr.rahul`, **not** their full name or e-mail address, because the name is written to the log) and their own **authenticator app** (Google Authenticator, Microsoft Authenticator, Authy, 1Password, or any TOTP app):
  ```
  python scripts/make_hr_password_hash.py add priya --new-secret                  # the first user (also prints a SESSION_SECRET)
  python scripts/make_hr_password_hash.py add rahul --map '<the current HR_USERS value>'   # add a person
  python scripts/make_hr_password_hash.py add rahul --map '<current>'             # again for the same name: change the password (the phone stays)
  python scripts/make_hr_password_hash.py add rahul --map '<current>' --reset-totp   # a new phone: new authenticator secret
  python scripts/make_hr_password_hash.py remove rahul --map '<current>'          # remove a person
  ```
  - It asks for the password twice (at least 12 characters, not shown). For a new person (or `--reset-totp`) it then makes an **authenticator secret** and prints an **`otpauth://` address** and, if you ran `pip install qrcode` first, a **QR code in the terminal**. The person scans the QR code (or types the secret) into their app, and you type the **6-digit code the app shows**. **Nothing is printed for storing until that code matches**, so a mis-scanned secret is caught here.
  - It then prints the **whole new `HR_USERS` value**. **Paste it straight into the SSM console. Do not put it in chat, email or git.** The authenticator secret is in that value and on your screen while you enrol: do it on your own computer, and show the QR code only to the person it is for.
  - **Sign-in needs three things together:** the login name, the password and the current 6-digit code. The code is accepted if it is the current 30-second code or the one just before or after (a phone clock a little out). A wrong password and a wrong code give the **same message**, both count toward the lock-out, and **a code works once** (the same code a second time is refused).
  - The AWS deployment **refuses to start** if any user has no authenticator secret (`--no-mfa` exists only to try the portal on a laptop).
  - A removed person can no longer sign in after the HR function next starts (to sign them out at once, also replace `SESSION_SECRET`, which signs **everybody** out).
- Create them in the console (Systems Manager → Parameter Store → **Create parameter** → type **SecureString**, KMS key `alias/hr-onboarding`), **so the secret is not typed into a terminal history**. The CLI form is:
  ```
  aws ssm put-parameter --name /hr-onboarding/portal/GEMINI_API_KEY --type SecureString --key-id alias/hr-onboarding --value "<paste>"
  ```
- Non-secret settings (bucket names, table names) are ordinary **function environment variables** (Step 17), not SSM.
- Never paste a secret into chat, email or git. If one leaks, problem I.
- **Cost: free** for standard parameters *(not re-checked)*; the KMS requests are inside the free amount.

### Step 17. The two Lambda functions

Create the log groups first (Step 6 did that). Create each function from the image. **Architecture arm64, memory 1024 MB, timeout 60 seconds.**

**17a. The employee portal**
```
aws lambda create-function --function-name hr-onboarding-portal \
  --package-type Image --code ImageUri=<ECR_URI>:v1 --architectures arm64 \
  --role arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-portal-role \
  --memory-size 1024 --timeout 60 \
  --image-config '{"Command":["python","-m","hr_onboarding.employee_portal"]}' \
  --logging-config LogFormat=Text,LogGroup=/aws/lambda/hr-onboarding-portal \
  --environment 'Variables={STORAGE_BACKEND=dynamodb_s3,PORT=8000,AWS_REGION=<REGION>,S3_BUCKET=igtl-hr-analytics-ayush-2026,S3_DOCUMENTS_BUCKET=igtpl-hr-onboarding-docs-2026,S3_RESTRICTED_BUCKET=igtpl-hr-onboarding-restricted-2026,S3_PREFIX=onboarding/,DDB_APPLICATIONS_TABLE=hr-onboarding-applications,DDB_FORMS_TABLE=hr-onboarding-forms,DDB_COUNTERS_TABLE=hr-onboarding-counters,SSM_PARAMETER_PREFIX=/hr-onboarding/portal/,SSM_PARAMETER_NAMES=GEMINI_API_KEY,LOG_LEVEL=INFO}'
```
**17b. The HR portal**: the same, with `--function-name hr-onboarding-hr`, `--role .../hr-onboarding-hr-role`, `--image-config '{"Command":["python","-m","hr_onboarding.hr_portal"]}'`, log group `/aws/lambda/hr-onboarding-hr`, and these variables: `PORT=8001`, `SSM_PARAMETER_PREFIX=/hr-onboarding/hr/`, `SSM_PARAMETER_NAMES=HR_USERS,SESSION_SECRET`, `ONBOARDING_URL=https://<ONBOARDING_DOMAIN>` (and the same bucket, table and `S3_PREFIX` variables).

Notes:
- **`AWS_REGION`** is also set by Lambda itself; setting it is harmless. **`S3_KMS_KEY_ID` is not set** (the buckets use SSE-S3). **`SHARED_TABLE_PREFIX` is not set** (empty = the top-level `employees/` and `employee_education/` folders the live Athena tables expect).
- **Log format:** `LogFormat=Text` makes Lambda keep the app's own JSON lines exactly as written (`levelname`, `employee_id`, no personal data). Debug logging is **off** unless you set `LOG_LEVEL=DEBUG`; do not.
- **`AWS_REGION` and `S3_DOCUMENTS_BUCKET` also build the address the Content-Security-Policy allows** (`https://<BUCKET>.s3.<REGION>.amazonaws.com`), so both must be set on both functions.
- **Settings come from the environment, secrets from SSM.** A missing or unreadable secret stops the function at start with a message that names the parameter, never its value. A missing bucket setting stops it with `Unsafe storage configuration, refusing to start` (Appendix C).
- **Function URL, locked to IAM** (CloudFront is the only caller, Step 18):
  ```
  aws lambda create-function-url-config --function-name hr-onboarding-portal --auth-type AWS_IAM --invoke-mode BUFFERED
  aws lambda create-function-url-config --function-name hr-onboarding-hr --auth-type AWS_IAM --invoke-mode BUFFERED
  ```
  Write down each `FunctionUrl`. With `AWS_IAM`, a direct call without a CloudFront signature is refused.
- **Reserved concurrency: not set by default, on purpose.** First **check your account's quota**: console → **Service Quotas** → **AWS Lambda** → **Concurrent executions** → read **Applied account-level quota value**. A new account often has **10**, and AWS requires 10 to stay unreserved, so you could not reserve any. Request an increase there if you want a cap. **Only if the value is well above 10** (for example 100 or more) may you cap each function to protect cost and block floods, for example `aws lambda put-function-concurrency --function-name hr-onboarding-portal --reserved-concurrent-executions 10`. **Do not do that unless you also read Step 19**: the night switch uses the same setting, and the 7 am schedule removes the cap.
- **Test the function before the front door exists** (it cannot be called from the internet yet): console → the function → **Test** with a small event, or `aws lambda invoke`. A cold start of a few seconds is normal; look at the log group for `Started` lines and any `ERROR`.
- **Cost:** about $0 at this volume (free tier). *(Not re-checked.)*

### Step 18. The front door: CloudFront (HTTPS, the night page, and the only way in)

Two CloudFront distributions, one per name, each with one small **CloudFront Function** that runs before anything else. Everything here is done in the console (or by the administrator); the CLI forms are long.

**18a. Certificates.** Console → **Certificate Manager** → **switch the region to `us-east-1` (N. Virginia)**. CloudFront can only use certificates from there. **Request** a public certificate for both `<ONBOARDING_DOMAIN>` and `<HR_DOMAIN>`, DNS validation. Add the validation record in DNS (Route 53 has a button). Wait for **Issued**. This is `<CERT_ARN>`.

**18b. One origin access control.** Console → CloudFront → **Origin access** → Create: name `hr-onboarding-lambda-oac`, **origin type Lambda**, signing **Sign requests** (SigV4).

**18c. The two CloudFront Functions.** CloudFront → **Functions** → **Create function**, runtime **cloudfront-js-2.0**:
- **`portal-night-page`**: paste `deploy/cloudfront/portal-viewer-request.js`. Publish.
- **`hr-office-and-night`**: paste `deploy/cloudfront/hr-viewer-request.js`, **edit the `OFFICE_IPS` list at the top** to the office's address(es) (one IPv4 address, or a range like `203.0.113.0/24`). Publish.
- **What they do.** Both close the portal from **00:00 to 06:59 India time**: they answer by themselves with **HTTP 503**, a small inline HTML page ("The portal is available 7 am - 12 am IST") and `Cache-Control: no-store`, and the request never reaches Lambda. The time check works on India time directly (UTC+05:30, no daylight saving), so it does not depend on any server's clock setting. The HR Function **also** lets only the office addresses through (everyone else gets a plain **403**, even at night, so a stranger learns nothing). Neither Function touches cookies or login headers: the HR sign-in is a form and a signed cookie inside the app (`hr_login.py`).
- Both files are tested (`tests/test_cloudfront_functions.py`, in a real JavaScript engine) at the boundaries 23:59 (open), 00:00 (closed), 06:59 (closed) and 07:00 (open).
- To change the hours later, change `< 7` in `isClosed` in **both** files and the cron times in Step 19.

**18d. The portal distribution** (employees). **Create distribution** with these settings:
- **Origin:** domain = the portal's Function URL host (`xxxx.lambda-url.<REGION>.on.aws`, without `https://`), protocol **HTTPS only**, origin access control `hr-onboarding-lambda-oac`.
- **Default behaviour:** viewer protocol **Redirect HTTP to HTTPS**; allowed methods **GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE**; cache policy **CachingDisabled**; origin request policy **AllViewerExceptHostHeader** (the Function URL has its own host name; this policy also forwards cookies); **Function association: Viewer request = `portal-night-page`**.
- **Alternate domain name:** `<ONBOARDING_DOMAIN>`; custom certificate `<CERT_ARN>`; security policy **TLSv1.2_2021** or newer; **HTTP/2 and HTTP/3** on; price class **Use North America, Europe, Asia, Middle East, and Africa** (it includes India).
- **No response headers policy is needed.** The two apps send the security headers themselves on every response, including errors: **`Content-Security-Policy`**, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security` (one year) and `Permissions-Policy: camera=(self), microphone=(), geolocation=()` (the page uses the camera for photos). The policy is strict: `default-src 'self'; script-src 'self'; connect-src 'self' https://<BUCKET>.s3.<REGION>.amazonaws.com; img-src 'self' data: blob:; frame-ancestors 'none'` (plus `base-uri 'self'; form-action 'self'`), with **no `unsafe-inline` and no `unsafe-eval`**. The pages are served as files of their own (`/assets/...`, built in Step 14) with React and the photo cropper copied into the project, so nothing loads from another site. The HR page's policy differs in two places: its `connect-src` is only `'self'` (it uploads nothing) and its `img-src` and `frame-src` also name the documents bucket address, because a document opens through a 5-minute S3 link. (If you also want a CloudFront response headers policy, it must not set a different `Content-Security-Policy`.)
- **No custom error pages are needed.** The night page comes from the Function, not from an error response.
- Create it. Copy the **distribution ID** (`<PORTAL_DIST_ID>`) and its `dxxxx.cloudfront.net` name.

**18e. Let CloudFront call the function** (otherwise you get a 403 "AccessDeniedException"). Run for the portal (and again for the HR function with `<HR_DIST_ID>` once 18f exists):
```
aws lambda add-permission --function-name hr-onboarding-portal --statement-id AllowCloudFrontUrl \
  --action lambda:InvokeFunctionUrl --principal cloudfront.amazonaws.com \
  --source-arn arn:aws:cloudfront::<ACCOUNT_ID>:distribution/<PORTAL_DIST_ID> --function-url-auth-type AWS_IAM
aws lambda add-permission --function-name hr-onboarding-portal --statement-id AllowCloudFrontInvoke \
  --action lambda:InvokeFunction --principal cloudfront.amazonaws.com \
  --source-arn arn:aws:cloudfront::<ACCOUNT_ID>:distribution/<PORTAL_DIST_ID>
```
(Recent AWS documentation asks for **both** actions on a function URL. If only the first is accepted in your account, the second can be skipped; if calls still fail with 403, add it.)

**18f. The HR distribution**: the same as 18d with these differences:
- Origin is the **HR** function's URL; alternate domain `<HR_DOMAIN>`; **Function association: Viewer request = `hr-office-and-night`**.
- **IPv6 off** (Settings → IPv6), because the office allow-list is IPv4.
- Run 18e for the HR function with `<HR_DIST_ID>`.
- **How the HR sign-in works through CloudFront:** the portal shows its own **sign-in form** (`/login`). A correct password sets one cookie (HttpOnly, Secure, SameSite=Strict, 8 hours, signed). CloudFront forwards cookies (the origin request policy above) and passes `Set-Cookie` back. The `Authorization` header is **not** used (CloudFront's origin access control replaces it with its own signature, which is why the earlier HTTP-Basic login could not work here).

**18g. Docs bucket CORS** (so the browser may send a file straight to S3 from your page, and from no other site). Save as `cors.json`, then `aws s3api put-bucket-cors --bucket <BUCKET> --cors-configuration file://cors.json`:
```json
{"CORSRules": [{
  "AllowedOrigins": ["https://<ONBOARDING_DOMAIN>"],
  "AllowedMethods": ["POST"],
  "AllowedHeaders": ["*"],
  "ExposeHeaders": ["ETag"],
  "MaxAgeSeconds": 600
}]}
```
Only the **employee** domain is listed. HR opens documents through ordinary links (no CORS needed). The app's S3 client signs links for the **regional** address `https://<BUCKET>.s3.<REGION>.amazonaws.com` (not the older global `s3.amazonaws.com`, which redirects and would break both this CORS rule and the page's Content-Security-Policy).

**18h. DNS.** Route 53 (or the company DNS team): `<ONBOARDING_DOMAIN>` → **alias/CNAME** to the portal distribution's `dxxxx.cloudfront.net`; `<HR_DOMAIN>` → the HR distribution's name.

**18i. Test.** Do it in the order of **Part 11A** (First deployment test checklist). In short: the form opens, the Function URL itself returns 403, HR shows the sign-in form from the office and Forbidden from elsewhere, and a test submission works.
- **Cost:** about $0. *(Not re-checked.)*

### Step 19. The 12 am to 7 am shutdown: two layers

**Layer 1, the friendly page (Step 18c).** From 00:00 to 06:59 India time each CloudFront Function answers every request itself with the "available 7 am - 12 am IST" page (HTTP 503). It needs no schedule: it reads the clock.

**Layer 2, the hard off switch (this step).** At **00:00 India time** an EventBridge schedule sets **reserved concurrency to 0** on both functions, so Lambda **refuses to run them at all**, even if someone found a way round CloudFront. At **07:00** another schedule **removes** the limit (`DeleteFunctionConcurrency`), so the functions run again and scale freely. (The 7 am schedule does **not** set the concurrency back to a number such as 10; Step 17 explains the optional cap.) If layer 1 is ever misconfigured, layer 2 still keeps the portals off (the visitor then sees a plain error instead of the friendly page).

Four schedules (time zone **`Asia/Kolkata`**, so there is no daylight-saving confusion), all using the role `hr-onboarding-scheduler-role`:

| Schedule | Time | Action on function |
|---|---|---|
| `hr-portal-off` | `cron(0 0 * * ? *)` | `putFunctionConcurrency`, `{"FunctionName":"hr-onboarding-portal","ReservedConcurrentExecutions":0}` |
| `hr-hr-off` | `cron(0 0 * * ? *)` | `putFunctionConcurrency`, `{"FunctionName":"hr-onboarding-hr","ReservedConcurrentExecutions":0}` |
| `hr-portal-on` | `cron(0 7 * * ? *)` | `deleteFunctionConcurrency`, `{"FunctionName":"hr-onboarding-portal"}` |
| `hr-hr-on` | `cron(0 7 * * ? *)` | `deleteFunctionConcurrency`, `{"FunctionName":"hr-onboarding-hr"}` |

One of them as a command (repeat for the others, changing the name, time, target and input):
```
aws scheduler create-schedule --name hr-portal-off \
  --schedule-expression "cron(0 0 * * ? *)" --schedule-expression-timezone Asia/Kolkata \
  --flexible-time-window Mode=OFF \
  --target '{"Arn":"arn:aws:scheduler:::aws-sdk:lambda:putFunctionConcurrency","RoleArn":"arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-scheduler-role","Input":"{\"FunctionName\":\"hr-onboarding-portal\",\"ReservedConcurrentExecutions\":0}"}'
aws scheduler create-schedule --name hr-portal-on \
  --schedule-expression "cron(0 7 * * ? *)" --schedule-expression-timezone Asia/Kolkata \
  --flexible-time-window Mode=OFF \
  --target '{"Arn":"arn:aws:scheduler:::aws-sdk:lambda:deleteFunctionConcurrency","RoleArn":"arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-scheduler-role","Input":"{\"FunctionName\":\"hr-onboarding-portal\"}"}'
```

**Test layer 1 (the page) without waiting for midnight.** A CloudFront Function's **Test** tab uses the real time, so by day it shows the request passing through. To see the night page by day: open the Function, change `return ist.getUTCHours() < 7;` to `return ist.getUTCHours() < 24;`, **Publish**, wait for the distribution to finish deploying, open the address (you must see **"The portal is available 7 am - 12 am IST"**, status 503), then **change it back to `< 7` and Publish again**. Do not forget the revert: the portal would stay closed. (Part 11A repeats this as a test.)

**Test layer 2 (the hard switch):**
1. Switch a function off by hand: `aws lambda put-function-concurrency --function-name hr-onboarding-portal --reserved-concurrent-executions 0`. A request that reaches the function now fails with a throttle error (a plain "Rate Exceeded" through CloudFront).
2. Switch it on: `aws lambda delete-function-concurrency --function-name hr-onboarding-portal`. The site works again within seconds.
3. Then run each schedule once: EventBridge → Scheduler → the schedule → check **Last invocation** the next morning, and `aws lambda get-function-concurrency --function-name hr-onboarding-portal` (no output = no limit = on).

**To work at night on an urgent day** you must open **both layers**, and close them again afterwards:
1. Remove the hard switch: `aws lambda delete-function-concurrency --function-name <name>`.
2. Open the Function: change `< 7` to `< 0` in `isClosed` (never closed), **Publish**.
3. **When you finish:** put `< 7` back and Publish, and run `aws lambda put-function-concurrency ... 0` again (the next scheduled "off" is not until midnight, and the layer-1 page only helps in the closed hours).
To skip a whole night properly you can also disable the two "off" schedules (EventBridge → Scheduler → **Disable**) and **enable them again the next day**.

**To change the hours later:** change the hour in `isClosed` in both Function files (and publish), and the two cron times here (hours `0` and `7`).

- **Cost: free** (CloudFront Function invocations are inside the free amount; the first 14 million scheduler invocations a month are free) *(not re-checked)*.
- **What the night switch does not do:** it stops **new** requests. DynamoDB, S3 and CloudFront stay on (and cost almost nothing).

### Step 20. Athena and Glue (SQL on the HR tables), then move the old data

- **First ask your colleague** (Step 10j, question 4): do the Athena tables from `samples/hr_tables/athena_tables.sql` already exist on `igtl-hr-analytics-ayush-2026`? If **yes**, do not create them again: the new `employees/IGTPL###.parquet` and `employee_education/IGTPL###.parquet` files are picked up by their existing tables, and you only need permission to query them. Skip to the Test line. If **no**, create them yourself as below.
- Console → **Glue** → Databases → **Add database** `hr_analytics` (skip if it exists).
- **Athena** → Workgroups → **Create** `hr-onboarding`: result location `s3://<BUCKET>/athena-results/` (your own bucket, not the shared one), tick "Override client-side settings", per-query data limit 10 GB.
- In the query editor choose workgroup `hr-onboarding`, open `samples/hr_tables/athena_tables.sql`, replace `YOUR_BUCKET` with `igtl-hr-analytics-ayush-2026`, and run each `CREATE TABLE`. The tables point at the **top-level** folders `s3://igtl-hr-analytics-ayush-2026/employees/` and `.../employee_education/` (no `hr/` prefix). The **bank and sensitive tables must point at `s3://<BUCKET_RESTRICTED>/hr/...`** (your private bucket), not the shared one.
- Test: `SELECT employee_id, full_name, designation, employment_status FROM hr_analytics.employees WHERE employee_id LIKE 'IGTPL%';`
- Optional **Lake Formation** (ask the administrator): grant SELECT on the two restricted tables only to the HR-admin role.
- **Rebuild the tables from the records** at any time with `python scripts/sync_tables.py` (with `STORAGE_BACKEND=dynamodb_s3` and the `S3_*` and `DDB_*` variables set, from a computer that is signed in to AWS with permission to read the tables and write the table folders). It never deletes a file.
- **Moving old data from MongoDB (only if there is an earlier MongoDB deployment; skip on a fresh start).** The one-time script `scripts/migrate_mongo_to_dynamodb.py` copies the old `applications` and `forms` into the DynamoDB tables and sets the ID counter to the highest `IGTPL` number so new IDs continue from it. **Read these rules first:**
  - It is a **dry run unless you add `--apply`**: it only reports what it would do. Run the dry run first and read it.
  - It is **safe to run again**: an item that already exists is never overwritten, and the counter only moves up.
  - It prints employee IDs only, never a value from a record.
  - It needs `pip install pymongo`, the old `MONGODB_URI` (a secret: set it in your terminal only, never in a file) and AWS access with write permission on the three tables. The documents and the HR tables are already in S3 and are not touched.
  ```
  python scripts/migrate_mongo_to_dynamodb.py            # dry run
  python scripts/migrate_mongo_to_dynamodb.py --apply    # really copy
  ```
  After checking the new tables in the app, delete the old MongoDB cluster and any copy of its connection string (and see Part 7.2 about retention).
- **Move data that was only ever on a laptop** (the old `submissions/`, `forms/` and `uploads/` folders): `python scripts/sync_tables.py --from-local` with the `dynamodb_s3` variables set copies each student's record and form into DynamoDB, their documents into `onboarding/employees/<id>/documents/`, and writes the table files. Then **delete the local `uploads/` and `submissions/` folders** and any zip of the project from CloudShell: they hold Aadhaar and PAN.
- **Cost: Athena $5 per TB scanned (megabytes = cents); Glue free at this size.**

### Step 21. Lock down the two private buckets (documents and restricted)

Do this **last**, after everything works. It makes the buckets answer **only** the two function roles and named administrators, even if someone later gives a user broad S3 access by mistake. **A wrong deny rule locks everyone out, including you (Part 9, problem J), so test the exact policy on a throwaway bucket first** (create `hr-onboarding-test-<random>`, apply the policy with your own role in the allowed list, check that you and a test role can still read, and a third user cannot).

Add this statement to the bucket policy of **both** `<BUCKET>` (documents) and `<BUCKET_RESTRICTED>`, next to the HTTPS-only statements from Step 10e (`aws s3api get-bucket-policy` first, then add; do not replace). Change the role list per bucket as shown below the JSON:
```json
{"Sid": "OnlyTheAppAndNamedAdmins", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
 "Resource": ["arn:aws:s3:::<NAME>", "arn:aws:s3:::<NAME>/*"],
 "Condition": {"StringNotLike": {"aws:PrincipalArn": [
   "arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-portal-role",
   "arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-hr-role",
   "arn:aws:iam::<ACCOUNT_ID>:user/<IAM_USER>",
   "arn:aws:iam::<ACCOUNT_ID>:role/<COMPANY_ADMIN_ROLE>"]}}}
```
- **The pre-signed uploads and downloads still work**, because a pre-signed link is checked as the role that made it (the portal role for uploads, the HR role for downloads), and both are on the list.
- Add any other principal that legitimately reads the bucket: the **Athena users' role** on `<BUCKET_RESTRICTED>`, the **Macie** and **AWS Backup** service roles if you use them, and the **CloudTrail/Config** roles are not needed (they use the logs bucket).
- S3's own lifecycle rules and the access-logging delivery are not affected by a bucket policy.
- If you are locked out: problem J.

---
## Part 7. Extra security and privacy (turn on once the app works)

### 7.1. Extra security services

**Ask the administrator first**: in a company account GuardDuty, Config, Security Hub, and an organization CloudTrail are often already run centrally. Do not duplicate them. Prices are in Part 2.3.

- **Macie (optional):** Console → Macie → Enable → create a **discovery job** on `<BUCKET>` and `<BUCKET_RESTRICTED>` (run weekly or monthly, $1.25 per GB scanned). Expected matches: the ID documents under `onboarding/employees/` and the restricted tables. **Not expected:** any match in the shared bucket's `employees/` or `employee_education/` (ask your colleague to scan it; problem K).
- **GuardDuty:** Console → GuardDuty → Enable → Settings → turn on **S3 Protection** (and **Lambda Protection** if offered). Send "High" findings to `hr-onboarding-alerts` (EventBridge rule → SNS).
- **CloudTrail data events for the DynamoDB tables** (who read or changed an application): add this selector to the trail next to the S3 one from Step 5. It **replaces** the trail's event settings, so it repeats the management and S3 selectors; check in the console that all three show as enabled:
  ```
  aws cloudtrail put-event-selectors --trail-name hr-onboarding-audit-trail --advanced-event-selectors '[
   {"Name":"Management events","FieldSelectors":[{"Field":"eventCategory","Equals":["Management"]}]},
   {"Name":"S3 HR data","FieldSelectors":[{"Field":"eventCategory","Equals":["Data"]},{"Field":"resources.type","Equals":["AWS::S3::Object"]},{"Field":"resources.ARN","StartsWith":["arn:aws:s3:::igtpl-hr-onboarding-docs-2026/","arn:aws:s3:::igtpl-hr-onboarding-restricted-2026/"]}]},
   {"Name":"DynamoDB HR tables","FieldSelectors":[{"Field":"eventCategory","Equals":["Data"]},{"Field":"resources.type","Equals":["AWS::DynamoDB::Table"]},{"Field":"resources.ARN","StartsWith":["arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/hr-onboarding-"]}]}]'
  ```
  **Cost:** $1 per million events (cents at your volume) *(DynamoDB rate not re-checked)*. A scan of the applications table by HR is one event, not one per record.
- **AWS Config:** record all resources (or daily recording to save money), store in `<BUCKET_LOGS>`. Managed rules: `s3-bucket-public-read-prohibited`, `s3-bucket-ssl-requests-only`, `s3-bucket-versioning-enabled`, `s3-bucket-logging-enabled`, `cloudtrail-enabled`, `dynamodb-pitr-enabled`, `dynamodb-table-encrypted-kms`, `lambda-function-public-access-prohibited`. "Noncompliant" means a setting changed or was missed.
- **IAM Access Analyzer:** IAM → Access Analyzer → Create analyzer (account). Free.
- **AWS Backup (optional):** Backup → Create plan `hr-daily` (02:00, keep 35 days), resources = both data buckets. (DynamoDB already has PITR for 35 days; add the tables here only if you need a longer history.)
- **HR sign-in:** each HR person has their **own login, password and authenticator app** (`HR_USERS`, Step 16): the form asks for all three together. Every sign-in, list view, application view, document open, approve, reject and flag is logged with their **login name** (`hr_user`) and the employee ID, so each view of a record is attributable to a person. The protection is the office-IP rule, a password stored only as a hash, a **one-time code (TOTP, RFC 6238)**, the 8-hour cookie and the lock-out (a wrong code counts like a wrong password). What it does not give: **hardware keys or push approval**, a single sign-on with the company directory, or a record of who an HR person is beyond their login name. Ask the company SSO team about single sign-on before a client with stricter rules uses it.
- **WAF:** not used (Part 2.3). Add it, with a rate rule, if a client needs bot protection.
- **Tags** on every resource: `Project=hr-onboarding`, `Environment=prod`, `DataClass=PII`, plus any tag the company requires.

### 7.2. Privacy and data protection

**Legal points are marked "confirm with legal". They are not legal advice.**

**Data map: where each kind of personal data lives**

| Data | Where | Who can read it |
|---|---|---|
| Name, father's name, date of birth, mobile, email, permanent and current address, family, education, employment history, references | DynamoDB `hr-onboarding-applications` (the record) and `hr-onboarding-forms` (the form as submitted) | portal role (write; one record by ID), HR role (read; the whole table) |
| **PAN number, Aadhaar number, bank account number and IFSC** | The same two tables, and the restricted tables `hr/employee_sensitive_details/` and `hr/employee_bank_details/` in `<BUCKET_RESTRICTED>` | The two roles, and the named administrators (Step 21) |
| **Scans: Aadhaar front and back, PAN, photo, bank proof, payslips, relieving letter, resume, certificate** | `<BUCKET>` under `onboarding/employees/<ID>/documents/`, and for up to one day a second copy under `onboarding/pending/<random id>/` (the waiting folder the browser uploads into; a lifecycle rule removes it) | Portal role (write and check), HR role (read through 5-minute links) |
| Name, date of birth, gender, designation, employment status; education rows | The **shared** bucket `igtl-hr-analytics-ayush-2026` (`employees/`, `employee_education/`): this is your colleague's bucket and Athena database | Everyone your colleague allows: **no PAN, Aadhaar, bank or scans go there** |
| The same documents, **sent to Google (Gemini)** so the AI can read them | Google's service, when the employee presses "read my documents" | Google, under the terms of your Gemini plan (below) |
| Employee ID only (never a name, number or file content) | CloudWatch logs of the two functions, CloudTrail (S3 keys contain the ID) | Anyone who can read the logs |
| Old versions of files and records | S3 versions (365 days), DynamoDB PITR (35 days), AWS Backup if used | Administrators |

**Encryption**
- **In transit:** HTTPS from the browser to CloudFront (TLS 1.2+), HTTPS to S3 (a bucket policy refuses plain HTTP, Step 10e), HTTPS to DynamoDB and Gemini.
- **At rest:** DynamoDB with your KMS key (rotation on); SSM secrets with the same key; S3 with SSE-S3 (the pre-signed upload **forces** `x-amz-server-side-encryption: AES256`, so a file cannot be stored unencrypted). If a client wants S3 encrypted with their own key, that is a per-bucket change plus a colleague agreement for the shared bucket (Step 10c).
- **Not encrypted by the app:** the PAN, Aadhaar and account numbers are stored as plain text **inside** encrypted storage. See the Aadhaar note below.

**Access**
- Two roles, no keys, no delete (Step 15). The buckets answer only those roles and named administrators (Step 21). The employee portal cannot list or scan anything; HR can scan the applications table, and only after signing in **and** from the office IP.
- Documents are never public. HR sees them through a link that **expires in 5 minutes** and is made only after the login. The employee's upload link also lasts 5 minutes, names exactly one file, and allows only the right type and size.
- An employee's edit link carries a secret token. It **does not expire by itself**. Once HR approves or rejects, the link can no longer change the application, but it still shows the status and the employee's own documents. Treat the link like a password. (An expiry is a code change for later.)

**In the browser**
- Both pages run under a strict **Content-Security-Policy** (no inline code, no code from other sites, no `eval`), `nosniff`, no referrer and HTTP Strict Transport Security, all set by the apps themselves. Pages cannot be put in a frame. A cross-site script that finds its way into a page could not load more code or send data to any site except the documents bucket (employee page).

**Logging**
- Logs contain the **level, the module, a short message and the employee ID**; on the HR portal also the **HR user's login name** (`hr_user`, a login such as `priya`, never a person's name or e-mail address) on every sign-in, list view, application view, document open, approve, reject and flag. They never contain a name, number, address, password or file content, and an error is logged as its type and file and line only (`jsonlog.py`). A **failed** sign-in is logged without the name that was typed (someone may type a password or an e-mail address there). Debug logging is off. The web access log is off (an edit link in a URL would be recorded).
- CloudTrail data events show who read which file and which table.

**Retention and deletion**
- **Decide the retention period** (for example: rejected applications deleted after 6 months, joiners kept for the employment plus the legal minimum). **Confirm with legal.** Write it in the company policy and in the notice below.
- **To delete one person** (a deletion request, or the retention period ended). The roles cannot delete, so an administrator does it:
  1. DynamoDB: delete the item `employee_id = <ID>` from `hr-onboarding-applications` and `hr-onboarding-forms` (turn deletion protection off only for the table if the console needs it, never delete the table).
  2. `<BUCKET>`: delete `onboarding/employees/<ID>/` **with all versions** (`aws s3api list-object-versions --bucket <BUCKET> --prefix onboarding/employees/<ID>/`, then delete each version and delete marker).
  3. `<BUCKET_RESTRICTED>`: the same for `hr/employee_bank_details/<ID>.parquet` and `hr/employee_sensitive_details/<ID>.parquet`.
  4. The shared bucket: `employees/<ID>.parquet` and `employee_education/<ID>.parquet`. **The app cannot delete there: ask your colleague.**
  5. Check with `list-object-versions` that nothing is left.
- **What still exists afterwards, until it ages out:** DynamoDB point-in-time recovery keeps the deleted item's history for **up to 35 days**; S3 old versions are removed by the lifecycle rule after 365 days; **delete the versions yourself** (step 2) instead of waiting; backups (AWS Backup, if used) until their own expiry; CloudTrail/CloudWatch records keep the **ID** (no personal data). **Tell the person this in the notice (confirm with legal).**
- **Files an employee replaced or removed** while fixing a flagged application stay in S3 (the portal role cannot delete). They are not referenced and HR does not see them, but they still exist. Include them in the periodic cleanup: compare the files under each `onboarding/employees/<ID>/documents/` with the `documents` list in that record, and delete the extra ones (administrator, monthly).
- **Abandoned uploads:** an employee who uploads and never submits claims **no ID** (the ID is claimed only at submit). Their files wait in `onboarding/pending/<random id>/` and the S3 lifecycle rule (Step 10f) removes them **after 1 day**, including old versions; no role needs to delete anything. A submitted application also leaves its waiting copies there for that one day: they vanish by themselves.

**Aadhaar (confirm with legal).** Indian rules limit how an organisation may collect, display and store the **Aadhaar number**. The safe, common practice is to **store only the masked number (the last 4 digits)** and keep the full number only if the law requires it, and then in an **Aadhaar Data Vault** (a separate, tightly controlled store that keeps the full number behind a reference token). Today the app stores the **full 12-digit number** in the record and in the restricted table, and the scan of the card in S3. **Do not change what is collected without legal's decision**: a code change to store the masked number only is small, but it changes the schema, so it needs your approval first.

**Consent notice (India's Digital Personal Data Protection Act 2023; confirm with legal).** Before the employee gives details, the form should show a short notice in plain words saying: **what** is collected (including PAN, Aadhaar, bank details, scans), **why** (HR onboarding and payroll only), **who** sees it (HR; that the documents are read by an AI service of Google), **where and how long** it is kept, and **how to ask for correction or deletion** and **whom to complain to** (the grievance contact). The form already has a "declaration" box; the notice text and consent record need legal's wording. **Write the notice and add it before a real launch.**

**Correction and deletion requests (confirm with legal).** Name one person who receives them (the grievance officer). **Correction:** HR flags the field and the employee fixes it through their link (this is built in). **Deletion:** follow the steps above and reply to the person within the time legal sets. Keep a note of each request (date, ID, what was done) in an HR register, **not** in the application.

**Breach.** Problem G has the steps. Under the DPDP Act a personal data breach has to be reported (to the Data Protection Board and the affected people): **ask legal for the deadline and wording in advance**.

**The Gemini API (confirm with legal and the company).** Documents leave AWS and go to Google. Use a **paid** plan whose terms say prompts are not used to improve Google's products, **set a spending cap on the key**, and say so in the notice. The app sends **one document set per request and returns only the extracted fields**; it never sends the whole record and never stores or logs the bytes.

---

## Part 8. Total cost, and how to lower it

**Option A, about 7 to 9 applications a day, Mumbai, before 18 % GST.** The S3, KMS, CloudWatch, SNS, CloudTrail and Route 53 lines follow the price files of 2026-11-11; the **Lambda, DynamoDB, CloudFront, ECR and totals are planning estimates, not re-checked**. Confirm in the AWS Pricing Calculator.

| Line item | What it covers | Per month |
|---|---|---|
| Lambda | two functions, about 300 requests a day, 1024 MB, 60 s limit (inside the free tier) | **$0** to $0.30 |
| DynamoDB | three on-demand tables, PITR on | **$0.05** to $0.50 |
| S3 | storage (well under 1 GB) and requests, your three buckets | **$0.20** to $0.60 |
| CloudFront | two distributions, two small Functions (inside the always-free tier) | **$0** |
| **WAF** | **not used**: the HR address is limited by a CloudFront Function. (A WAF would add about **$5 + $1 per rule** a month) | **$0** |
| KMS | one key $1.00, requests inside the free amount | **$1.00** to $1.30 |
| SSM Parameter Store | three standard parameters | **$0** |
| CloudWatch | log ingestion and storage (JSON lines, well under 1 GB), 8 to 10 alarms | **$0.60** to $1.50 |
| ECR | one image of about 0.5 GB | **$0.05** to $0.10 |
| CloudTrail | first trail free; S3 and DynamoDB data events (cents) | **$0.05** to $0.30 |
| EventBridge Scheduler, ACM, Glue, SNS, Athena | free or cents | **$0** to $0.10 |
| Route 53 (skip if the company DNS team does it) | one hosted zone | **$0.50** |
| **AWS total** | | **about $3 to $5** (about $2.5 to $4.5 if the company does DNS) |
| Gemini API | billed by Google, not AWS; set a spending cap | see the Google AI pricing page |
| Optional: GuardDuty, Macie, Config, Backup | Part 2.3 | +$10 to $30 |

- Set the AWS budget (Step 2) at **$15** until you see a real bill (the bill should be a quarter of that). Review **Billing → Bills** every week for the first month.
- **Everything that costs money while nobody uses the app** is small: KMS $1, CloudWatch alarms, DynamoDB PITR, S3 storage, Route 53. Lambda, CloudFront and the Function cost nothing when idle.
- **If the total looks too high:** drop the optional security services first (use the company's central ones), reduce alarms, keep log retention at 90 days, and skip Route 53 if the company runs DNS.
- **WAF decision:** the brief mentioned WAF on the HR address. It would cost about $5 a month plus about $1 per rule, more than the whole bill without it. **This guide does not use WAF**: the HR address is limited to the office IP by the CloudFront Function, and behind it the portal has its own sign-in form (password stored only as a hash in SSM, a one-time code from the person's authenticator app, a signed 8-hour cookie, and a lock-out after 5 wrong tries). Someone on the office network now needs both the password **and** that person's phone. Add WAF, or per-person logins, if a client needs more.
- Costs that grow with use: Lambda time (slow document reading), CloudWatch log ingestion ($0.67 per GB: keep debug logging off), CloudTrail data events, Athena scans, S3 storage, the Gemini API.
- **Data transfer out** to the internet is billed per GB after the free monthly amount (CloudFront's first 1 TB is free); employee documents are small. *(Rate not re-checked.)*

---
## Part 9. What if something goes wrong? (Troubleshooting and recovery)

**Golden process for any problem**

- Step 1: **Stay calm. Do not delete things or change many settings at once.** Write down the time and what you see.
- Step 2: Check **where to look** (Step 8): the Lambda log group in CloudWatch for app errors, CloudTrail Event history for AWS changes.
- Step 3: Find the problem below. Fix **one thing**, then test.
- Step 4: Write a short note: what happened, what fixed it. It becomes your own runbook.
- Step 5: Anything you cannot fix with your IAM user's permissions goes to the **company AWS administrator**, with the exact error text, the time, and what you tried.

### A. The website does not open, or shows an error page

- **Is it between 12 am and 7 am IST?** The functions are switched off on purpose; you should see "The portal is available 7 am - 12 am IST" (problem T if you do not).
- **During the day, the page shows "403 Forbidden" or "The request could not be satisfied":**
  - On the **HR** address: your office IP changed. Edit `OFFICE_IPS` at the top of the CloudFront Function `hr-office-only` and **Publish** it (Step 18f). Also check you are not on IPv6 or a VPN.
  - A 403 only when you press a button (pages load fine): problem Z (the content-hash header).
  - The function's permission for CloudFront is missing (Step 18e): `aws lambda get-policy --function-name hr-onboarding-portal` must show the distribution in `AWS:SourceArn`.
- **"502 Bad Gateway" / "Internal Server Error"** (the function started but the app failed): open the log group `/aws/lambda/hr-onboarding-portal` (or `-hr`) and read the newest lines. The usual causes:
  - `Unsafe storage configuration, refusing to start`: a function setting is missing, or two buckets are the same. The message names the variable (Appendix C).
  - `HR_USERS is not set`, `HR_USERS is not valid JSON`, `HR_USERS has a user name that is not ...`, `HR_USERS has a password that is not a hash ...`, `HR_PASSWORD is set` or `SESSION_SECRET is missing or shorter than 32`: fix the SSM parameters (Step 16; make `HR_USERS` with `scripts/make_hr_password_hash.py`); the next cold start picks them up (to force it, change any function setting, or publish a new version).
  - `SSM parameters not found or not readable: GEMINI_API_KEY`: the parameter name, the `SSM_PARAMETER_PREFIX`, or the role's `ssm:GetParameters` / key permission is wrong (problem X).
  - `ModuleNotFoundError`: the `Dockerfile` does not copy a module the app imports; fix the `COPY` line and release a new image (problem L).
  - `exec format error`: the image was built for the wrong CPU. Rebuild with `--platform linux/arm64` (Step 14).
  - "Runtime exited" or "Task timed out after 60 seconds" at start: problem Y.
- **Still failing:** go back to the previous image (problem L) and tell the administrator.

### B. The browser says "certificate" or "not secure"

- The certificate must be in **`us-east-1`**, status **Issued**, and cover **both** names (Step 18a). Check CloudFront → the distribution → **Alternate domain names** and the certificate.
- The DNS name must point to the distribution's `dxxxx.cloudfront.net` name (`nslookup <ONBOARDING_DOMAIN>`).
- An expired validation: ACM renews by itself as long as the DNS validation record stays in DNS. Do not delete it.

### C. "AccessDenied", "not authorized", or "403" when saving or reading files

- Read the full error text. It names the action (for example `s3:PutObject`) and the resource, and **who** was denied.
- **If a function was denied** (the log shows its role): check in order:
  - The role is missing the permission (Step 15). Common: `s3:GetObject` for the file check, `dynamodb:Scan` for HR, `ssm:GetParameters`.
  - The role is not allowed on the key (error mentions `kms:`): problem X.
  - **The denied bucket is the shared one** (`igtl-hr-analytics-ayush-2026`): if it is in another account, your colleague has not added (or has changed) the bucket policy from Step 10j. Send them the exact error.
  - The bucket name in `S3_BUCKET` or `S3_DOCUMENTS_BUCKET` has a typo or is in another region.
  - **The lock-down rule (Step 21)** does not list the role: add it. A deny always beats an allow.
- **If you were denied** (CloudShell says `User: ...:user/<IAM_USER> is not authorized to perform: X`): your IAM user lacks a permission. Send the **exact message** to the administrator and ask for that permission on those resources only.
- **"explicit deny in a service control policy":** the company blocks this action for everyone in the account. Only the administrator can change it.
- Find what was denied: CloudTrail → Event history → filter "Event name" = the action, look for `errorCode: AccessDenied`.

### D. Slow, or an upload or "read my documents" fails

- Check the log for `ERROR` lines at that time (Step 8). Gemini errors are logged as the error type only.
- **Slow first request after a quiet period:** problem Y (cold start).
- **"These files are too large to read together":** the page sent more than about 5.5 MB in one reading request (a Lambda request is capped at 6 MB). The page shrinks big photos by itself; ask the employee to use smaller photos or fill the fields by hand. The **real upload** to S3 is not affected (up to `MAX_UPLOAD_BYTES`, 5 MB per file).
- **Out of memory** (`Runtime exited with error: signal: killed`, or "Max Memory Used" near 1024 MB in the `REPORT` line): reading large PDFs is the heavy part. Raise the function's memory: `aws lambda update-function-configuration --function-name hr-onboarding-portal --memory-size 2048`. It costs proportionally more only while it runs.
- Check the Gemini API: if the key is wrong or the quota is exhausted, the form checks fail. Rotate the key if needed (problem I).

### E. Data is missing in Athena or the HR portal

- A failed table write is logged as `HR table sync failed` **but the submission is still saved in DynamoDB**. Look for `ERROR` in the portal log (most often AccessDenied on the shared bucket, problem C).
- Run `python scripts/sync_tables.py` (Step 20) to rebuild all the tables from the records.
- Employee missing in the **HR portal**: the portal reads DynamoDB, not Athena. Check the `hr-onboarding-applications` item for the ID. An item with `application_status = Draft` is an ID that was claimed but whose submit failed; it is hidden on purpose and removes itself after 7 days.
- In Athena, make sure the workgroup is `hr-onboarding` and the database is `hr_analytics`. The wrong region in the console also shows empty tables.
- Athena error "HIVE_..." or "schema mismatch": re-run the table's `CREATE TABLE` from `athena_tables.sql`.
- Athena error "S3 access denied": your user needs `s3:GetObject` (and Lake Formation permission). Ask the administrator (problem C).

### F. A file or a record was deleted or overwritten by mistake

- **A file:** versioning is on, so nothing is really gone. Console → S3 → bucket → open the folder → toggle **Show versions**. A deleted file shows a "Delete marker". Select the **delete marker** and click **Delete** (the file returns). For an older version of a changed file, download it and upload it again.
- CLI: `aws s3api list-object-versions --bucket <BUCKET> --prefix onboarding/employees/IGTPL007/` shows version IDs. Remove a delete marker with `aws s3api delete-object --bucket <BUCKET> --key <KEY> --version-id <DELETE_MARKER_ID>`.
- Find who deleted it: CloudTrail data events (`DeleteObject`) or the Logs Insights query in Step 8. Versions older than 365 days are removed by the lifecycle rule, so recover quickly.
- **A record changed or deleted in DynamoDB** (the function roles cannot delete, so this was an administrator or a mistake): use **point-in-time recovery**. Console → DynamoDB → the table → **Backups** → **Restore to point in time** → choose a time before the mistake → restore **into a new table** (for example `hr-onboarding-applications-restored`). Read the item there, and copy it back into the live table. Then run `python scripts/sync_tables.py` to rewrite the table files. Never restore over the live table without the administrator. PITR reaches back **35 days**.
- **A file deleted in the shared bucket:** the app has no delete permission there, so this came from someone else. Ask your colleague to restore it from their bucket versions, or run `python scripts/sync_tables.py` to rewrite the student's files from the records.

### G. I think data was seen or taken by the wrong person (possible breach)

- **Contain first, investigate second.** Within the first hour:
  - **Switch both portals off now**: `aws lambda put-function-concurrency --function-name hr-onboarding-portal --reserved-concurrent-executions 0`, the same for `hr-onboarding-hr`. (Then **disable the two morning schedules** in EventBridge, or they will switch the portals back on at 7 am.)
  - **Call the administrator and your manager now.** As an IAM user you cannot see or fix everything; they must disable suspect users or keys.
  - If a bucket was exposed: S3 → bucket → Permissions → make sure **Block all public access is On**.
  - If a key leaked: problem I.
- **Preserve evidence:** do not delete logs. CloudTrail, S3 access logs and CloudWatch logs are your proof of what happened and when.
- **Find out what happened:** CloudTrail Event history and the Logs Insights queries in Step 8 (S3 and DynamoDB data events). Check GuardDuty and Macie findings if enabled.
- **Tell the right people:** the HR head and the legal or compliance owner immediately. Under India's DPDP Act a personal data breach has to be reported to the Data Protection Board and to the affected people (**confirm with legal**: the deadline and the wording). Do not wait for the investigation to finish.
- **Recover:** rotate every secret (Gemini key, HR password), review all IAM changes in the last 90 days, restore anything modified (problem F), switch the app on again, re-enable the schedules, and write down the cause and the fix.

### H. Surprise bill or a budget alert arrived

- Billing → **Cost Explorer** → group by **Service**, last 7 days. The top service is the cause. (Ask the administrator for access if you cannot see it.)
- Typical causes and fixes:
  - **Lambda** high: a flood of requests or very slow requests. Look at CloudFront's request count and the Lambda `Duration` graph. Set reserved concurrency (Step 17, only if your quota allows) and consider WAF (Part 7.1). Check that debug logging is off.
  - **CloudWatch Logs** high: something logs too much ($0.67 per GB). Fix the app log level, keep retention set.
  - **DynamoDB** high: something scans the table in a loop. Check CloudTrail data events for who.
  - **CloudTrail data events** high: a script is reading many files or items. Check who in the logs.
  - **Macie** high: a discovery job is scanning too often ($1.25 per GB). Make it weekly or monthly.
  - **EC2 / NAT gateway / Elastic IP / ALB** lines: leftovers from an earlier design (a NAT gateway alone is about $40 a month). Check EC2, VPC and Load Balancers in **all regions** and delete what you do not use.
  - **Unfamiliar services or regions**: possible leaked keys, treat as problems I and G.
- Delete things you do not use.

### I. A password, API key or AWS access key was leaked (pasted in chat, committed to git, emailed)

- **Rotate right away** (deleting is not enough, assume it was copied). Edit the SSM parameter (Parameter Store → the parameter → **Edit**), then make the functions pick it up by changing any setting on the function (for example `aws lambda update-function-configuration --function-name hr-onboarding-portal --description "rotated"`), which starts fresh copies:
  - Gemini key: create a new one in Google AI Studio, store it in `/hr-onboarding/portal/GEMINI_API_KEY`, delete the old key.
  - HR password: run `scripts/make_hr_password_hash.py add <user> --map '<current HR_USERS>' --new-secret`, replace `/hr-onboarding/hr/HR_USERS` **and** `/hr-onboarding/hr/SESSION_SECRET` (a new secret signs everybody out at once, which is what you want after a leak). If the whole map leaked, change every person's password.
  - An old **MongoDB** string that is still around: delete the old cluster's user or the cluster (Step 20).
  - AWS access key or your password: tell the administrator; they deactivate it. You can also do IAM → your user → Security credentials → **Make inactive**, then delete. (The functions have no access keys: they use roles.)
- If the key was in git, rewriting history is not enough. Treat it as public forever.

### J. I locked myself out (bad bucket policy, lost MFA, permission removed)

- **Bucket policy deny locked everyone out:** you cannot fix it with an IAM user if the deny covers you. Ask the administrator or the account owner (root user) to delete or edit the bucket policy. Say which bucket and when it changed (Step 21 is the usual cause).
- **Lost MFA device:** the administrator resets it (IAM → user → Security credentials → remove the old device) and you add a new one.
- **"Access denied" on things that worked yesterday:** the administrator may have changed your permissions or a company policy. Ask.

### K. Macie or Config reports PAN or Aadhaar in the wrong place, or a bucket is no longer encrypted/blocked

- Open the finding to see the exact object key.
- Move the file to the right bucket and prefix, or delete it, and fix the code or process that put it there.
- For a Config "Noncompliant" item, re-apply the setting from Step 10 and check CloudTrail Event history to see who changed it.

### L. A new version broke the portal: roll back

- Each release is a new image tag (`v1`, `v2`...), and tags cannot be reused (Step 14). To go back, point the function at the previous good tag:
  ```
  aws lambda update-function-code --function-name hr-onboarding-portal --image-uri <ECR_URI>:v1
  aws lambda update-function-code --function-name hr-onboarding-hr --image-uri <ECR_URI>:v1
  ```
  Wait for `LastUpdateStatus` to be `Successful` (`aws lambda get-function --function-name hr-onboarding-portal --query Configuration.LastUpdateStatus`), test the site and read the log.
- **To release a new version:** build and push a new tag, update both functions to it, test. Do it outside working hours, and test locally first (`python -m hr_onboarding.employee_portal`).

### M. A function, a table or the image was deleted or broken

- **A function:** create it again from the image (Step 17), re-add the permission for CloudFront (Step 18e). The Function URL changes: update the CloudFront origin. Nothing is stored in a function.
- **A DynamoDB table:** deletion protection should have stopped it. If it is gone, **restore from the latest PITR/backup** into a table with the same name (Step 13 settings), then run `python scripts/sync_tables.py`.
- **The image:** rebuild and push it (Step 14).
- Keep the Step 17 commands and the environment values in the company's secret store or runbook so this takes minutes.

### N. A whole region or service has an outage

- Check https://health.aws.amazon.com/health/status and the **Health Dashboard** in the console.
- Nothing to fix on your side. Tell HR the portals are temporarily down. Data in S3 has 99.999999999 % durability and is stored across several data centres in the region.
- To protect against a whole-region failure, set up cross-region replication and DynamoDB global tables in another region (check data-residency rules first). This doubles storage cost.

### O. I deleted a whole bucket, a KMS key, or the trail

- **KMS key scheduled for deletion:** KMS → key → **Cancel key deletion** (7 to 30 day waiting period). After deletion, **DynamoDB and the secrets cannot be read again**.
- **Bucket deleted:** the data is gone. Restore from **AWS Backup** or a replica if you set one up (Part 7.1).
- **CloudTrail stopped:** CloudTrail → Trails → **Start logging**. Check who stopped it in Event history. The alarm in Step 7 should have emailed you.

### P. The schedule did not work: the app is ON at night or OFF in the daytime

- EventBridge → **Scheduler** → Schedules: all four (`hr-portal-off`, `hr-hr-off`, `hr-portal-on`, `hr-hr-on`) must exist, be **Enabled**, and say the time zone `Asia/Kolkata`. A failed invocation shows in the schedule's monitoring; the usual cause is the role (Step 15c) or `iam:PassRole`.
- Check the live state: `aws lambda get-function-concurrency --function-name hr-onboarding-portal`. A value of `0` means **off**; no output means **on**.
- **Fix the state immediately by hand:** on = `aws lambda delete-function-concurrency --function-name <name>`; off = `aws lambda put-function-concurrency --function-name <name> --reserved-concurrent-executions 0`. Then fix the schedule.
- If someone set a reserved concurrency of **10** earlier, the 7 am schedule removes it. That is intended (Step 19).

### Q. I cannot do a step because my IAM user lacks permission

- This is normal in a company account. Do not look for tricks.
- Copy the exact message, the step number from this guide, and the permission you need (Part 0.4 has the list). Send it to the administrator and wait for the grant. Ask for everything in one go.

### R. I need to look inside a running function

- You cannot open a shell in Lambda. Use the **logs** (Step 8). To try something, **invoke the function with a test event** in the console (Test tab), or run the app on your laptop with `STORAGE_BACKEND=local`.

### S. DynamoDB problems: throttling, "ValidationException", or empty results

- `ProvisionedThroughputExceededException` / `ThrottlingException` on an on-demand table: a very sudden burst. The app's AWS client retries by itself; if it persists, look for a loop (Cost Explorer, CloudTrail).
- `ResourceNotFoundException`: a table name in the function settings (`DDB_*`) is wrong, or the table is in another region.
- `AccessDeniedException ... dynamodb:Scan` from the **portal**: expected; the portal must not scan. From **HR**: add the permission (Step 15b).
- The list shows nobody but records exist: they are `Draft` (a failed submit), or the HR role reads the wrong table (check `DDB_APPLICATIONS_TABLE` on `hr-onboarding-hr`).
- `ValidationException ... Item size has exceeded the maximum allowed size`: a record over 400 KB (a normal one is about 3 KB). Look at what was submitted.

### T. The night page does not appear, or the portal is closed by day / open at night

- **At night you see a plain "Rate Exceeded" or an error, not the friendly page:** the CloudFront Function is not answering. Check CloudFront → Functions → `portal-night-page` (or `hr-office-and-night`) → **Associated distributions**: it must be attached as a **Viewer request** on the **default behaviour**, and **Published** (an edited but unpublished Function does nothing). A change takes a few minutes to reach every edge; wait until the distribution says **Deployed**. The plain error then comes from layer 2 (concurrency 0), which is working.
- **At night the portal still works:** both layers failed to close it. (1) The Function is not attached, or someone edited `isClosed` (look for `< 24` or `< 0`, Step 19). (2) The concurrency limit is not 0: `aws lambda get-function-concurrency --function-name hr-onboarding-portal` should print `0` at night (problem P).
- **By day the portal shows the closed page:** the Function was left edited (`< 24`) after a test. Put `< 7` back and Publish. Or the clock: the Function uses India time, so check the time in India, not on your computer.
- **The page is still there after 7 am:** the Function's answer is never cached (`no-store`), so it is an old browser tab. Reload it.
- **A 429 in the daytime means the function is throttled:** (1) `aws lambda get-function-concurrency` returns `0`: the 7 am schedule did not switch it on (problem P); (2) you set a reserved concurrency and a burst used it all: raise it, or remove it (`delete-function-concurrency`); (3) the **account** limit is reached (Service Quotas → Lambda → Concurrent executions): request an increase.

### U. The pre-signed upload fails with 403 or "The upload link expired"

- The files go to a **waiting folder**, `onboarding/pending/<random id>/<name>`, and nothing about the employee exists yet. An upload link lasts **5 minutes** and works for **one file name**. If the employee took longer, or the phone slept, press **Submit** again: the page asks for new links for the **same** waiting folder (no ID is used up).
- A 403 from S3 with `<Code>AccessDenied</Code>` and `Invalid according to Policy: Policy Condition failed` names the failed condition in the message:
  - `content-length-range`: the file is empty or over 5 MB.
  - `Content-Type`: the browser sent a different type from the one allowed (`image/jpeg` for photos and cards, `application/pdf` for PDFs). The page converts other photo formats (HEIC, PNG, WebP) to JPEG before this; a custom client or a proxy that rewrites the type breaks it.
  - `x-amz-server-side-encryption`: the form was changed.
- `SignatureDoesNotMatch` or `ExpiredToken`: the **portal role's** temporary credentials ended before the link did (the link works only as long as the role's session). Retry; a new request uses a new copy.
- `AccessDenied` that is **not** a policy message: the portal role lacks `s3:PutObject` on `onboarding/pending/*` (Step 15a), or the lock-down rule (Step 21) does not list the role (problem C).
- After a successful upload the **submit** says "... was not received": the waiting file is missing, empty, too large or the wrong type, or the lifecycle rule already removed it (waiting files live **one day**). Re-upload by pressing Submit again with the files still chosen.
- Submit says "Your documents could not be saved. Please press Submit again": the copy from `pending/` to the employee's folder failed (usually a missing `s3:PutObject` on `onboarding/employees/*` or `s3:GetObject` on `pending/*`, Step 15a; read the log for the error type). HR sees nothing yet. Fix the permission, then press Submit again; the retry takes the next ID number (a gap, never a duplicate).

### V. The browser shows a CORS error when uploading

- The browser console says `has been blocked by CORS policy` on the S3 address. The docs bucket's CORS (Step 18g) must list **exactly** the page's address in `AllowedOrigins` (`https://<ONBOARDING_DOMAIN>`, no trailing slash, `https` not `http`) and the method `POST`. A test from `http://localhost` is blocked on purpose.
- `aws s3api get-bucket-cors --bucket <BUCKET>` shows what is set. Changes apply within a minute; reload the page.
- CORS errors on the **CloudFront** address itself do not occur: the page and the API share one address.

### W. `ConditionalCheckFailedException`, or "This application could not be matched to its uploads"

- This is the safety net, not a bug: a **create-only** write found the record already there, or a **replace** found no record. Typical causes:
  - **The employee pressed Submit twice** (or reloaded after sending): the first one already created the record. Look up the ID in the HR portal; do not create another.
  - **"could not be matched to its uploads"** means the page sent no valid upload id (an old or modified page). Reload the page and submit again: the chosen files are still in the browser.
  - **The counter and the table disagree** (records were moved in by the migration script): the app skips taken IDs by itself.
- An ID is claimed only when the submit has passed every check, so an unfinished form never causes this. The log line has the employee ID only. **Never fix it by deleting the item.**

### X. KMS `AccessDeniedException` (a function cannot use the key)

- The error mentions `kms:Decrypt`, `kms:GenerateDataKey` or `kms:DescribeKey`, from DynamoDB or SSM.
- Check in order: (1) the role policy has the `Key` statement with the **exact key ARN** (Step 15); (2) the key's policy still has the default "Enable IAM User Permissions" statement (the key policy must not have been replaced); (3) the key is **Enabled**, not pending deletion (problem O); (4) you are in the same region as the key.
- Test the role from the administrator's side with the IAM Policy Simulator on `kms:Decrypt`.

### Y. Cold start, or "Task timed out after 60.00 seconds"

- **Cold start:** the first request after a quiet period (and the first one after 7 am) starts a new copy: loading the app, then reading the SSM secrets once. It takes a few seconds; later requests are fast. If it is too slow, the cheapest fix is to leave it. **Provisioned concurrency** (a paid, always-warm copy) fixes it but costs more than the whole rest of the bill: do not use it unless a client insists.
- **Timeout:** the log shows `Task timed out after 60.00 seconds`. The slow part is Gemini reading several large documents. The employee sees an error; pressing the button again usually works. If it is frequent: raise the function memory (more CPU too, problem D) and check the Gemini status page. The timeout can be raised up to 15 minutes, but CloudFront gives up after its own origin timeout (30 seconds by default; 60 with a quota increase), so a longer function timeout does not help the user.

### Z. A button gives 403 through CloudFront (the content-hash header), or the HR sign-in does not work

- **POST, PUT and PATCH need `x-amz-content-sha256`.** With the origin access control, such a request must carry the **SHA-256 of its body** in the header **`x-amz-content-sha256`**, or Lambda rejects the signed request ("The request signature we calculated does not match"). The pages add this header themselves (`signedFetch` in `employee_portal.py`, `withBodyHash` in `hr_portal.py`, and the sign-in form). If pages load (GET) but buttons and the sign-in fail with a 403:
  - A browser that does not support `crypto.subtle` (an old browser, or the page opened over plain `http`): it cannot compute the hash. Use a current browser over HTTPS.
  - A custom client or a script that posts directly: add the header yourself (Part 11A, step 3 shows the command).
- **HR is asked to sign in again and again:** the cookie is not being kept. Check in the browser's developer tools (Application → Cookies): `hr_session` should exist with HttpOnly, Secure and SameSite=Strict. It is missing if the origin request policy of the HR distribution does not forward cookies (use **AllViewerExceptHostHeader**, Step 18d), if the address is opened over plain `http`, or if the browser blocks cookies for the site. After 8 hours the next click sends you to the form: that is intended.
- **"Wrong user name, password or code"** with the right ones: the form needs the login name, the password **and** the current 6-digit code, and gives the same message for any of them being wrong. Check, in this order: (1) the phone's clock is on **automatic time** (a clock more than a minute out fails every code); (2) you are using the entry for **this** person in the app (the app shows the label `HR Review Portal:<login name>`); (3) you are not typing the **same code twice** (a code works once: wait for the next one); (4) the hash and secret in `HR_USERS` belong to this person. If it still fails, run `scripts/make_hr_password_hash.py add <user> --map '<current>' --reset-totp`, scan the new QR code, and replace `HR_USERS` (Step 16). A lost phone is the same fix: replace that person's secret.
- **"Too many wrong attempts":** five wrong tries (a wrong password **or** a wrong code) lock that user name for 5 minutes, **in every copy of the function** (the count is an item `login#<user>` in the `hr-onboarding-counters` table). Wait five minutes, or an administrator sets `attempts` to 0 on that item. Names that do not exist share one item, `login#_unknown`, so guessing at names does not lock real people out. **"Sign-in is temporarily unavailable"** means the counter could not be read or written (DynamoDB or the HR role's `LoginLockoutOnly` statement, Step 15b): it refuses sign-ins on purpose rather than allow unlimited guessing; read the log for the error type.
- The HR sign-in does **not** use the `Authorization` header (CloudFront replaces it with its own signature), so a proxy or script that sends HTTP Basic credentials gets 401.

### AA. A page is blank, a button does nothing, or the browser console says "Content Security Policy"

- Open the browser console (F12). A line such as `Refused to load the script ... because it violates the following Content Security Policy directive` names what was blocked.
- **Blank page and the console says a script was refused:** `src/hr_onboarding/static/dist` is missing or the page links point at the wrong place. `https://<DOMAIN>/assets/index.app.js` (employees) or `.../assets/hr.app.js` (HR) must open as JavaScript. If it is a 404, the image was built without the first stage (Step 14): rebuild.
- **`Refused to connect to https://...s3...`:** the upload went to an address the policy does not list. The policy names `https://<BUCKET>.s3.<REGION>.amazonaws.com`, built from `S3_DOCUMENTS_BUCKET` and `AWS_REGION`; the pre-signed link must be the same address. A link for `s3.amazonaws.com` or another region means the function's `AWS_REGION` is wrong.
- **An HR document shows blank or "refused to display":** the link's address is not in the HR page's `img-src` / `frame-src` (same cause: `S3_DOCUMENTS_BUCKET`/`AWS_REGION`).
- **A browser extension** can also be blocked; test in a private window.
- **Photos from an iPhone:** the page converts PNG and WebP to JPEG itself. For HEIC it relies on the iPhone's photo picker handing over a JPEG, or on Safari decoding the file; other browsers cannot decode HEIC and no converter is shipped (it would need `unsafe-eval`). The employee is asked to choose a JPG.
- Never "fix" this by adding `unsafe-inline` or `unsafe-eval`; fix the cause.

### Who to contact in an emergency (fill this in and print it)

| Role | Name | Phone / email |
|---|---|---|
| You (AWS operator) | | |
| Backup operator | | |
| Company AWS administrator | | |
| HR head | | |
| Legal / compliance (DPDP grievance officer) | | |
| Management contact | | |

### Routine checks that prevent emergencies

- **Every day (2 minutes):** look for alarm emails. Open both portals once after 7 am.
- **Every week:** Billing → Bills for unexpected costs. The Lambda `Errors` and `Throttles` graphs. GuardDuty and Macie findings (if enabled).
- **Every month:** check that all four schedules ran (problem P). Clean up unreferenced uploads (Part 7.2). Ask the administrator to review who has access to `hr-onboarding-*`. Check that CloudFront Function `OFFICE_IPS` is still right.
- **Every quarter:** rebuild the image on the latest base (Step 14), release a new tag, and update both functions (problem L). Change the HR password and rotate the Gemini key (every 90 days).
- **Every year:** test a restore (one deleted file from versions, one table row from PITR). Review the data-retention period. Review this guide.

---

## Part 10. Final security checklist

Tick each one before real employee data goes in.

**Storage**
- [ ] Block Public Access is on for your three buckets (documents, restricted, logs)
- [ ] Buckets encrypted with SSE-S3 (and `S3_KMS_KEY_ID` empty); your own KMS key (rotation on) on DynamoDB and the secrets
- [ ] Bucket policy blocks plain HTTP and TLS below 1.2
- [ ] Versioning on and lifecycle rules set
- [ ] Bank and sensitive tables are **only** in `<BUCKET_RESTRICTED>`, and the ID documents are **only** in `<BUCKET>`. **Nothing but `employees` and `employee_education` rows is in `igtl-hr-analytics-ayush-2026`**
- [ ] Your colleague has confirmed who can read the shared bucket and its Athena tables
- [ ] No names, PAN or Aadhaar in S3 file or folder names
- [ ] The lock-down rule (Step 21) was tested on a throwaway bucket, then applied to both private buckets

**Database (DynamoDB)**
- [ ] The three tables: on-demand, KMS encryption, point-in-time recovery **on**, deletion protection **on**, TTL on `expires_at` for the applications table
- [ ] A test submission shows one item (not `Draft`) in `hr-onboarding-applications` and one in `hr-onboarding-forms`
- [ ] Two simultaneous submissions got two different IDs
- [ ] A restore of one item from PITR into a new table was practised once

**Access**
- [ ] MFA on your IAM user. You never use the root user
- [ ] The functions use roles. **No access keys anywhere**
- [ ] The portal role has **no Scan, no delete**; the HR role has Scan but **no delete**; nobody can delete in S3 (Step 15)
- [ ] The Function URLs use `AWS_IAM`; the bare Function URL in a browser returns **403**; CloudFront is the only way in
- [ ] The HR address returns **Forbidden** outside the office IP and shows the **sign-in form** inside it (a private window must never show the portal without signing in; the app also refuses to start without the sign-in settings)
- [ ] HR passwords exist **only as hashes** in SSM (`HR_USERS`, a SecureString); every HR user has an **authenticator secret** in it (the function refuses to start otherwise); the session cookie is HttpOnly, Secure, SameSite=Strict and expires after 8 hours (check in the browser's developer tools)
- [ ] A sign-in with the right password and a **wrong or reused code** is refused with the same message as a wrong password
- [ ] Five wrong passwords lock that user for 5 minutes in every copy of the function (try it once; the item `login#<user>` appears in the counters table)
- [ ] Both pages send `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy` and `Strict-Transport-Security`, and the browser console shows **no CSP violation** while every page is used (Part 11A)
- [ ] Both addresses show the closed page between 00:00 and 06:59 IST (Step 19 test)
- [ ] Each HR person has their own login in `HR_USERS`; a person who leaves is removed (and `SESSION_SECRET` replaced if they must be signed out at once); the log shows `hr_user` on every sign-in, view, approve, reject and flag
- [ ] The employee edit link carries a secret token in the URL: the app's web access log is off (do not turn it back on) and no log you add may record query strings
- [ ] Permission to see employee rows is checked in the backend. The AI model never decides it
- [ ] Your Gemini key is on a **paid** plan whose terms say prompts are not used to improve Google's products; a spending cap is set on the key
- [ ] The onboarding page is public and its AI-extraction calls are open: add rate limiting (WAF, Part 7.1) before a launch with many users
- [ ] Pre-signed links last 5 minutes (upload and download)

**Availability and cost**
- [ ] The four schedules exist, use `Asia/Kolkata`, and a test showed the friendly page at "night" and the portals back on after switching on
- [ ] The Lambda concurrency quota was checked (Step 17)
- [ ] Alarms (Step 7) confirmed by a test email
- [ ] Budget alert confirmed by a test email

**Logging and detection**
- [ ] CloudTrail `hr-onboarding-audit-trail` with S3 data events on both data buckets and DynamoDB data events on the tables
- [ ] The CloudWatch role `CloudTrailRoleForCloudWatchLogs-hr-onboarding` exists (Step 5), so the six security alarms work
- [ ] S3 access logs and Lambda logs, retention set (portal 90 days, HR 365 days); debug logging off
- [ ] Alarms from Step 7 confirmed by a test (trigger one and check the email)
- [ ] GuardDuty (with S3 protection) on, or confirmed covered by the company; Macie optional
- [ ] No PAN, Aadhaar, account numbers, or passwords in app logs (the logs hold the level, a message and the employee ID only)

**Data governance (India, DPDP Act 2023; confirm each with legal)**
- [ ] Purpose and consent notice written and shown on the form. Only what is needed is collected
- [ ] The Aadhaar decision (masked last 4 digits, or full number in a data vault) made by legal
- [ ] Retention period decided and enforced
- [ ] Deletion-request and correction process, and the grievance contact, written down (Part 7.2)
- [ ] Contact list (Part 9) filled in
- [ ] Data stays in `ap-south-1` (the CloudFront certificate in `us-east-1` is not data)

---

## Part 11. Verify the finished setup

- Public access blocked: `aws s3api get-public-access-block --bucket <BUCKET>` shows four `true` values.
- Encryption on: `aws s3api get-bucket-encryption --bucket <BUCKET_RESTRICTED>` shows `AES256`.
- Tables: `aws dynamodb describe-table --table-name hr-onboarding-applications` shows `ACTIVE`, KMS encryption and deletion protection; `aws dynamodb describe-continuous-backups --table-name hr-onboarding-applications` shows PITR `ENABLED`.
- The shared bucket is reachable by the roles: submit a test (below) and look for `employees/<ID>.parquet`.
- Functions: `aws lambda get-function-configuration --function-name hr-onboarding-portal --query "[Architectures,MemorySize,Timeout]"` shows arm64, 1024 and 60.
- HTTPS works: `curl -I http://<ONBOARDING_DOMAIN>` redirects to HTTPS; `curl -I https://<ONBOARDING_DOMAIN>/health` returns 200 with the `Strict-Transport-Security` header.
- The functions are reachable **only** through CloudFront: opening the Function URL directly returns 403.
- The HR portal is limited: from outside the office range, `https://<HR_DOMAIN>` returns **Forbidden**; from inside, it asks for the HR login.
- A test submission (open `https://<ONBOARDING_DOMAIN>`, fill the form with the "read my documents" step, submit) creates all of these:
  - an item in `hr-onboarding-applications` with the new ID (status `New`) and one in `hr-onboarding-forms`;
  - the student's own folder: `aws s3 ls s3://<BUCKET>/onboarding/employees/<ID>/documents/`;
  - the table files in the shared bucket: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/` shows `<ID>.parquet`;
  - the restricted files: `aws s3 ls s3://<BUCKET_RESTRICTED>/hr/employee_bank_details/` shows `<ID>.parquet`.
- **Upload rules:** try to upload a 6 MB file and a `.png` renamed to `.jpg`: both are refused with a clear message.
- Two students never share an ID: submit two applications at the same time from two browsers; both get different IDs.
- Sensitive tables are not in the shared bucket: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employee_bank_details/` returns nothing, while the same path in `<BUCKET_RESTRICTED>` lists files.
- HR fetches it: open `https://<HR_DOMAIN>` from the office, sign in, the student is listed with the documents (each opens through a link that works for 5 minutes; reload the link after 6 minutes and it must be refused). Approve it, then check `employees/<ID>.parquet` now says `Approved - Joining` (Athena: `SELECT employment_status FROM hr_analytics.employees WHERE employee_id='<ID>'`).
- The audit trail works: wait 5 to 15 minutes, then run the "who opened this file" query from Step 8.
- Athena works: `SELECT count(*) FROM hr_analytics.employees;`
- **Night test:** use each Function's **Test** tab in CloudFront with a time in the closed hours (Step 19), then check the real address the next night; switch a function off by hand to test the hard switch.
- **Schedule test:** the morning after, `aws lambda get-function-concurrency` for each function returns nothing (on) and the portal answered at 7 am.
- Alarm test: force an error (for example, call a page that raises) and confirm an alarm email arrives.
- Restore test: delete a test file, find the delete marker, restore it (problem F); restore one item from PITR into a new table.
- Negative test: sign in as a user without access and confirm the restricted bucket answers AccessDenied.

---

## Part 11A. First deployment test checklist

**None of this has run on real AWS yet.** The code was tested against fake AWS services and a local JavaScript engine. The items below are the parts that only real AWS can prove, **in the order to test them** (each step depends on the one before). Do them with a **throwaway test employee** and fake documents, before anyone real uses the portal. If a step fails, stop, fix it with the problem letter shown, and only then go on.

**1. Web Adapter cold start** (Steps 14 and 17; problems A, Y)
- [ ] In the Lambda console, open `hr-onboarding-portal` → **Test** with this event (the Function URL event format), then the same for `hr-onboarding-hr`:
  ```json
  {"version": "2.0", "rawPath": "/health", "rawQueryString": "", "headers": {"host": "test"}, "isBase64Encoded": false,
   "requestContext": {"http": {"method": "GET", "path": "/health", "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1"}}}
  ```
- [ ] Expect `"statusCode": 200` and a body `{"ok":true}`. Open the log group: JSON lines with `levelname`, no `exec format error`, no `Unsafe storage configuration`, no `SSM parameters not found`.
- [ ] Note **Init Duration** in the `REPORT` line (the cold start). A few seconds is normal; run the test again and it must be fast (a warm copy).

**2. HR login through CloudFront** (Steps 16, 18; problems A, Z)
- [ ] From the **office**, open `https://<HR_DOMAIN>/`: you land on the **sign-in form** (not a browser password pop-up).
- [ ] Sign in with the HR user and password. The employee list opens. In the browser's developer tools the cookie `hr_session` is **HttpOnly, Secure, SameSite=Strict**, expiring in 8 hours.
- [ ] Reload: still signed in. **Sign out**: back at the form; `https://<HR_DOMAIN>/api/employees` now returns 401.
- [ ] Each person **enrols their authenticator** (`scripts/make_hr_password_hash.py`, Step 16) and signs in with login name, password and the current 6-digit code. A wrong code gives "Wrong user name, password or code" (the same words as a wrong password), and the **same code a second time** is refused.
- [ ] A **wrong** password shows "Wrong user name, password or code"; **five** wrong ones (passwords or codes) then give "Too many wrong attempts". Check the counters table: an item `login#<user>` with `attempts` 5 and an `expires_at`, and after a good sign-in `last_step`.
- [ ] **The lock holds in every copy:** after the lock, wait for a different copy (open the HR page from a second browser, or update any function setting to start a new copy) and try the right password and code: it must still say "Too many wrong attempts" until the 5 minutes pass.
- [ ] **Two people, two logins:** a second HR user signs in with their own password and their own app; the CloudWatch log (`/aws/lambda/hr-onboarding-hr`) shows `hr_user` on the sign-in, the list view, an application view, a document open, and an approve and a flag. A failed sign-in shows **no** name, and **no code or secret** appears anywhere in the log.
- [ ] A phone with the **wrong time** fails and the same phone on automatic time works (this is the usual first-day problem).

**3. A POST with `x-amz-content-sha256` through the origin access control** (Step 18; problem Z)
- [ ] In the browser's Network tab, the `POST /login` and the HR "Approve" `POST` show a request header `x-amz-content-sha256`, and succeed (not 403).
- [ ] From a command line **at the office**, the header is what makes it work:
  ```
  BODY='{"user":"x","password":"y"}'
  HASH=$(printf %s "$BODY" | openssl dgst -sha256 -hex | sed 's/^.* //')
  curl -i -X POST https://<HR_DOMAIN>/login -H "Content-Type: application/json" -H "x-amz-content-sha256: $HASH" -d "$BODY"   # expect 401 from the app: {"error":"Wrong user name, password or code."}
  curl -i -X POST https://<HR_DOMAIN>/login -H "Content-Type: application/json" -d "$BODY"                                    # without the header: expect 403
  ```
- [ ] The Function URL address itself, opened directly, returns **403** (only CloudFront may call it).

**4. Pre-signed upload and CORS** (Steps 10f, 18g; problems U, V)
- [ ] Open `https://<ONBOARDING_DOMAIN>/`, choose the test documents (include a **PNG** and, if you have an iPhone, a **HEIC** photo: both must turn into a JPEG by themselves). Press Submit.
- [ ] Network tab: `POST /api/upload-urls` returns 200 and **no `employee_id`**; each file's `POST` to `...s3...amazonaws.com` returns **204** with no CORS error in the console.
- [ ] `aws s3 ls s3://<BUCKET>/onboarding/pending/ --recursive` shows the waiting files under one random id (before you submit, stop at this point once to look). Reload the page without submitting: `aws dynamodb get-item` on the counter shows **no ID was used**.
- [ ] A file renamed to the wrong type, or over 5 MB, is refused with a clear message. A request from another site (`curl -H "Origin: https://example.com" -X OPTIONS` on the S3 address) gets no CORS permission.
- [ ] The next day `onboarding/pending/` is empty (the 1-day lifecycle rule). **No role has delete**: confirm nobody added `s3:DeleteObject`.
- [ ] **Security headers and CSP:** `curl -sI https://<ONBOARDING_DOMAIN>/` (by day) shows `content-security-policy` with no `unsafe-inline` or `unsafe-eval`, `x-content-type-options: nosniff`, `referrer-policy: no-referrer` and `strict-transport-security`. In the browser console (F12) there is **no** `Content Security Policy` message while you go through every step of the form, **take a photo with the camera and crop it**, and upload a PNG. The page looks right (the Inter font is gone: the system font is used).
- [ ] **An iPhone photo:** from an iPhone pick a photo from the photo library: it must arrive as a JPEG and upload. A `.heic` file dragged on from a Windows PC is refused with "please choose a JPG" (expected).
- [ ] **HR viewer under the policy:** in the HR portal open a photo document (shown as an image) and a PDF (shown in the viewer frame): both appear, with no CSP message in the console. The link in the address bar for a download is `https://<BUCKET>.s3.<REGION>.amazonaws.com/...`.

**5. Full submit** (Steps 13, 15, 17; problems C, E, U, W, X)
- [ ] Finish the form and submit. The page shows the confirmation with `IGTPL###`.
- [ ] `aws dynamodb get-item` shows the item with status `New` in `hr-onboarding-applications` and one in `hr-onboarding-forms`; `aws s3 ls s3://<BUCKET>/onboarding/employees/<ID>/documents/` shows every file (copied from `pending/`).
- [ ] The table files exist: `employees/<ID>.parquet` in the shared bucket; `hr/employee_bank_details/<ID>.parquet` in `<BUCKET_RESTRICTED>`; **nothing** with bank or sensitive in the shared bucket.
- [ ] Two applications submitted at the same moment from two browsers get different IDs.
- [ ] In the HR portal (from the office) the application is listed; each document opens through a link; **the same link refuses after 6 minutes**.
- [ ] Approve it in HR; `employees/<ID>.parquet` now says `Approved - Joining`.

**6. Night page and the concurrency switch** (Steps 18c, 19; problems P, T)
- [ ] **Layer 1:** edit `isClosed` to `< 24` in `portal-night-page`, Publish, wait for **Deployed**, open `https://<ONBOARDING_DOMAIN>/`: you see **"The portal is available 7 am - 12 am IST"** with status 503. Do the same for `hr-office-and-night` from the office. **Put `< 7` back and Publish both.**
- [ ] **Layer 2:** `aws lambda put-function-concurrency ... --reserved-concurrent-executions 0`, then `delete-function-concurrency` (Step 19): off, then on again within seconds.
- [ ] Run each of the four schedules once (change its time to two minutes ahead, then set it back) and check the result with `aws lambda get-function-concurrency`.
- [ ] The next real night: at 00:00 both addresses show the page; at 07:00 they work again. `get-function-concurrency` prints nothing in the morning.

**7. CloudFront office-IP allow-list** (Step 18c, 18f; problem A)
- [ ] From the office: the HR address opens. From a phone on **mobile data**: **Forbidden** (and also at night: Forbidden, not the closed page).
- [ ] IPv6 is **off** on the HR distribution. Edit `OFFICE_IPS` to a wrong address, Publish: even you are refused; put it back.

**8. CloudTrail data events are visible** (Steps 5, 8; Part 7.1)
- [ ] After steps 4 to 5, wait 5 to 15 minutes. In Logs Insights on `/hr-onboarding/cloudtrail` (needs the CloudWatch role from Step 5) the "who opened a file" query shows the HR role's `GetObject` on the test employee's file, and the DynamoDB query shows the HR role's `Scan` and the portal role's `PutItem`.
- [ ] A test alarm email arrives (Step 7), and the Lambda `Errors` alarm has no data gaps.

When all eight are ticked, go back to Part 10 (the final checklist) and Part 11 (verify the finished setup).


## Part 12. Suggested order of work

- Day 1: Part 0 (sign in, MFA, CloudShell, send the request list). Wait for permissions. Steps 2 to 8 (budget, alerts, logs). **Do not skip.** Send your colleague the questions and bucket policy text in Step 10j.
- Day 2: Steps 9 to 11 (key, your buckets, the shared bucket answers), Step 12 (check the key), Step 13 (three tables), Step 14 (build and push the image), Step 15 (three roles), Step 16 (secrets).
- Day 3: Step 17 (the two functions, test them in the console), Step 18 (certificates, the two distributions, the two Functions, DNS, CORS), Step 20 (Athena; the data move if there is old data). Test the portals end to end.
- Day 4: Step 19 (night schedule and its test), Step 7 alarms, Part 7 (extra security, with the administrator), Part 10 (checklist), Part 11 (verify), then Step 21 (lock-down, tested on a throwaway bucket first), then the restore, night and alarm tests.
- Before a client's real launch: the privacy items in Part 7.2 (notice, Aadhaar decision, retention, deletion process) with legal.

## Part 13. About Terraform

- Terraform is **not used** for this deployment. The old `main.tf` described the previous ECS/MongoDB design and was removed from the project (it is in the git history, commit `18bb9f3`); it must not be applied, because it would clash with the shared bucket and the existing `hr_analytics` Glue database.
- If you later set up many clients, writing a Terraform or CloudFormation version of Steps 13 to 19 is the way to make each deployment repeatable. That is a new file.

---

## Appendix A. Containers elsewhere (Options B, C, D)

- The same Docker image runs anywhere that runs containers: set `STORAGE_BACKEND=dynamodb_s3` plus the `S3_*` and `DDB_*` variables from Appendix C, and give the container an IAM role (ECS task role, EC2 instance profile). The Lambda Web Adapter is only active on Lambda.
- **B (one EC2 server):** `docker-compose.yml` in the project starts the two portals; put Caddy or a load balancer in front for HTTPS and change the settings to the `dynamodb_s3` ones. The earlier Caddy and EC2 setup text was removed (git history, commit `18bb9f3`).
- **C, D (ECS Fargate behind a load balancer):** not built here. The earlier ECS steps are in the git history (commit `18bb9f3`, folder `archive/`) and describe the previous design: use them as a starting point only.

## Appendix B. Sources used for the prices and facts

- AWS Price List files (official, Mumbai region `ap-south-1`), read on 2026-11-11: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<service>/current/ap-south-1/index.json` for AmazonS3, awskms, AmazonCloudWatch, AmazonAthena, AWSCloudTrail, AmazonGuardDuty, AmazonMacie, AWSConfig, AWSBackup, AmazonSNS, AWSGlue, AmazonRoute53, AmazonVPC (public IPv4).
- AWS WAF pricing page: https://aws.amazon.com/waf/pricing/ (US rates; Mumbai not listed).
- AWS App Runner availability change (closed to new customers from 2026-04-30): https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html
- **Not re-checked, planning estimates:** **AWS Lambda** (requests, GB-seconds, the free tier), **DynamoDB** (on-demand and PITR), **CloudFront** (the free tier and Functions), **ECR**, **SSM Parameter Store**, **ACM**, **EventBridge Scheduler**, **IAM Access Analyzer**, AWS Budgets, CloudWatch alarm free tier, data transfer, **and every total and the Option B, C, D monthly figures in "Choosing the deployment option"**. The `$3-5` figure is the sum of the line items in Part 8, so it is only as good as those.
- **Behaviour that must be checked at the first real deployment** (the code was tested against fake AWS services and a local JavaScript engine only, never real AWS, and the pages have never been run in a browser under the Content-Security-Policy; Part 11A is the ordered checklist): the AWS Lambda Web Adapter on Lambda, the HR sign-in through CloudFront (cookies and the sign-in `POST`), CloudFront origin access control with a Lambda Function URL (including the `x-amz-content-sha256` header), the pre-signed upload with CORS and the regional S3 address in the pre-signed links, the Content-Security-Policy in a real browser (the compiled pages, the photo cropper, the HR document viewer), the sign-in lock-out kept in DynamoDB with the `LeadingKeys` IAM condition, the Docker build (both stages) for arm64, the CloudFront Functions (night page and office-IP rule), the EventBridge Scheduler concurrency switch, and CloudTrail data events.
- Facts about this app's storage (DynamoDB tables, S3 keys, the `S3_*`, `DDB_*` and `SSM_*` settings) come from the project's own `storage.py` and `hr_tables.py`.

## Appendix C. All app settings (environment variables)

| Variable | Meaning |
|---|---|
| `STORAGE_BACKEND` | `dynamodb_s3` (this deployment); `s3` (records as JSON in `S3_BUCKET`: **not for the shared bucket**, because each whole record, with PAN and Aadhaar, would be stored there); `mongo_s3` (**deprecated**, only so the migration script can read old data); `local` (default, files on disk, for a laptop) |
| `DDB_APPLICATIONS_TABLE`, `DDB_FORMS_TABLE`, `DDB_COUNTERS_TABLE` | DynamoDB table names. Defaults `hr-onboarding-applications`, `hr-onboarding-forms`, `hr-onboarding-counters`. The three must differ |
| `S3_BUCKET` | the shared bucket `igtl-hr-analytics-ayush-2026`: only the `employees` and `employee_education` tables |
| `S3_PREFIX` | prefix for the per-student folders, `onboarding/`. **Required** with `dynamodb_s3` and `s3` |
| `S3_DOCUMENTS_BUCKET` | **Required.** Your private bucket `<BUCKET>` for the uploaded documents. Must differ from `S3_BUCKET` |
| `S3_RESTRICTED_BUCKET` | **Required.** Your private bucket `<BUCKET_RESTRICTED>` for the bank and sensitive tables. Must differ from `S3_BUCKET` and `S3_DOCUMENTS_BUCKET` |
| `SHARED_TABLE_PREFIX` | folder in front of `employees/` and `employee_education/` in the shared bucket. Default empty (top level), matching the live Athena tables |
| `S3_KMS_KEY_ID` | encrypt **all** S3 writes with this KMS key. **Leave empty** (SSE-S3, which the shared bucket's Athena can read). If set, the pre-signed upload enforces KMS instead |
| `SSM_PARAMETER_PREFIX`, `SSM_PARAMETER_NAMES` | where to read secrets from SSM Parameter Store at start-up, e.g. `/hr-onboarding/portal/` and `GEMINI_API_KEY` (HR: `/hr-onboarding/hr/` and `HR_USER,HR_PASSWORD`). A real environment variable of the same name wins |
| `AWS_REGION` | region of the buckets and tables |
| `GEMINI_API_KEY`, `GEMMA_MODEL` | document reading (onboarding portal only); the key comes from SSM |
| `HR_USERS` | **Required** for the HR portal: a JSON map of HR login name → `{"hash": salted scrypt password hash, "totp": authenticator secret}`, made with `scripts/make_hr_password_hash.py` (a SecureString in SSM). Passwords are never stored. **The portal refuses to start if any user has no `totp`.** A bare hash string (no second factor) is accepted only on a laptop |
| `SESSION_SECRET` | **Required** for the HR portal: 32 or more random characters that sign the sign-in cookie (from SSM) |
| `HR_COOKIE_SECURE` | `1` (default): the cookie is marked Secure. `0` only to try the HR portal over plain `http` on a laptop |
| `ONBOARDING_URL` | public address of the onboarding portal, used in the HR portal's link to a flagged student |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO` (default; `DEBUG` only for a short test) and `json` (default; `text` for a laptop) |
| `HOST`, `PORT` | listen address (`0.0.0.0` in the image) and port (8000 portal, 8001 HR); on Lambda the Web Adapter forwards to this `PORT` |

The app **refuses to start**, with an error naming the variable (never a value), if `S3_DOCUMENTS_BUCKET` or `S3_RESTRICTED_BUCKET` is missing or empty, or equals `S3_BUCKET` (or each other), if `S3_PREFIX` is empty with `dynamodb_s3` or `s3`, if the three table names are not different, if an SSM parameter cannot be read, or if the HR portal has no valid sign-in settings (user, password hash, session secret) or is given a plain-text password.
