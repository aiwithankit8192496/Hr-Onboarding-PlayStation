# AWS Setup Guide: HR Onboarding (Containers on ECS Fargate, MongoDB + S3, Company IAM User)

Written for someone new to AWS who has been given a **company IAM user** (not the root account). It matches this project's code (`Dockerfile`, `storage.py`, `hr_tables.py`, `sync_tables.py`, `react_onboard.py`, `hr_info.py`).

**What this version is**

- The app runs as **Docker containers on Amazon ECS with AWS Fargate**. Fargate runs containers without you managing any server. Your existing `Dockerfile` is used **as it is** (no code changes, no Lambda adapter).
- Two containers from one image: the **onboarding portal** (`python react_onboard.py`, port 8000) and the **HR portal** (`python hr_info.py`, port 8001).
- **Where the data lives (`STORAGE_BACKEND=mongo_s3`):**
  - **MongoDB Atlas** holds each student's application record (one document per student, the student ID is its `_id`) and issues the student IDs from an atomic counter.
  - **S3** holds each student's uploaded documents in **their own folder** `onboarding/employees/<id>/documents/`, and the HR tables (same columns as `Data/sources_hr/athena_tables.sql`) as one Parquet file per student per table.
  - The HR tables go into the **shared analytics bucket `igtl-hr-analytics-ayush-2026`** (your colleague's bucket). The HR portal and Athena read from there. Bank and sensitive tables and the ID documents go to **your own private buckets**, not the shared one (Part 4).
- **Scalable:** the containers keep nothing on their own disk. ECS adds containers when CPU is high (Step 19), and any container can serve any student.
- The app is used **4 to 6 times a day** and must be **off from 12:00 am to 8:00 am (India time)**. The containers are stopped by a schedule at night, so you do not pay for compute while they are off. MongoDB and the data stay on.
- **Logging and alerts come first (Part 3)**, before any employee data exists.
- When something breaks, go straight to **Part 9**.

**About prices in this guide**

- Prices are **Mumbai (`ap-south-1`) prices in US dollars read from the official AWS Price List files and AWS pricing pages on 2026-11-11** (sources in Appendix B). They are exact for that date. They will change, so confirm in the AWS Pricing Calculator (https://calculator.aws) before you commit.
- Where a price could not be read from the AWS files, the line says **"not re-checked"**. Do not rely on those lines without checking.
- A month is counted as 30 days. The containers run 16 hours a day (8 am to midnight), which is **480 hours a month**. Servers that are always on (the load balancer) are counted at 730 hours a month.

Replace these placeholders everywhere:

| Placeholder | Example | Meaning |
|---|---|---|
| `<REGION>` | `ap-south-1` | Mumbai. Use the **same region for every service** (ask the administrator if the company restricts regions). |
| `<ACCOUNT_ID>` | `123456789012` | The company AWS account number (top-right menu in the console) |
| `<SHARED_BUCKET>` | `igtl-hr-analytics-ayush-2026` | Your colleague's analytics bucket. **Fixed: do not create it.** The app writes the HR tables here |
| `<BUCKET>` | `acme-hr-onboarding` | **Your** private bucket for the uploaded documents (Aadhaar, PAN, photo, resume...) |
| `<BUCKET_RESTRICTED>` | `acme-hr-onboarding-restricted` | **Your** private bucket for the bank and sensitive tables only |
| `<BUCKET_LOGS>` | `acme-hr-onboarding-logs` | Logs only |
| `<KMS_KEY_ARN>` | `arn:aws:kms:ap-south-1:...:key/...` | Your encryption key for secrets and logs (created in Step 9) |
| `<IAM_USER>` | `ankit.kumar` | Your company IAM user name |
| `<VPC_ID>`, `<PUBLIC_A>`, `<PUBLIC_B>`, `<PRIVATE_A>`, `<PRIVATE_B>` | `vpc-0abc...`, `subnet-0abc...` | A VPC with two public and two private subnets in different zones (Step 12) |
| `<NAT_IP>` | `13.x.x.x` | The fixed outbound address of the NAT gateway. MongoDB Atlas is told to accept only this address (Steps 12 and 21) |
| `<MONGODB_URI>` | `mongodb+srv://hr_app:...@cluster0.xxxx.mongodb.net/` | The MongoDB Atlas connection string (Step 21). A secret: never paste it in chat or git |
| `<DOMAIN>` | `onboard.example.com`, `hr.example.com` | Two web addresses you control (ask the company IT team for them) |

Bucket names are global across all of AWS, so pick unique ones for your own three buckets. The shared bucket name is your colleague's.

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
- CloudShell is an x86 machine. If you build the image for ARM (Step 13), do that on your laptop with Docker Desktop instead.

### Step 0.3. Find out what you are allowed to do

- Company accounts usually allow only part of what this guide does. You discover limits when you see **"is not authorized to perform ..."** or **"explicit deny in a service control policy"**.
- Quick tests in CloudShell (each should succeed or say AccessDenied):
  ```
  aws s3api list-buckets
  aws iam list-roles --max-items 3
  aws ecs list-clusters
  aws ecr describe-repositories --max-items 3
  aws elbv2 describe-load-balancers --max-items 3
  aws kms list-keys --max-items 3
  ```
- Anything denied goes on the **request list** below. Do not work around a denial. The restriction is deliberate and the administrator can grant it.

### Step 0.4. Send this request list to the AWS administrator

Ask them to give your user (or a role you can assume) these permissions, **restricted to resources whose names start with `hr-onboarding` or your bucket names** (that is what a least-privilege request looks like):

| Need | Permissions (service: actions) | Why |
|---|---|---|
| Storage | S3: create buckets, bucket policy, encryption, versioning, lifecycle, logging, public-access-block, objects on your three buckets | The data (Steps 4, 10) |
| Shared bucket | S3 on `igtl-hr-analytics-ayush-2026`: read and write only under `employees/`, `employee_education/` and `onboarding/` for the task role. This is the **bucket owner's** (your colleague's) permission, not yours. Part 4, Step 10j has the policy text | Step 10j, 14 |
| Encryption | KMS: create key, alias, rotation, key policy | Step 9 |
| Roles | IAM: `CreateRole`, `PutRolePolicy`, `AttachRolePolicy`, `PassRole` for roles named `hr-onboarding-*` only; `CreateServiceLinkedRole` for ECS and Application Auto Scaling (the first time) | Task roles (Step 14) |
| Containers | ECR: create repository, push images. ECS: create cluster, task definitions, services. Application Auto Scaling: register targets, scheduled actions | Steps 13, 16, 17, 19 |
| Network | EC2/VPC: create a VPC with public and private subnets, a **NAT gateway** and an **Elastic IP**, security groups (`hr-onboarding-*`). Elastic Load Balancing: create load balancer, target groups, listeners, rules | Steps 12, 18 |
| HTTPS | ACM: request certificates in `<REGION>`. Route 53 (or the company DNS team): create records for `<DOMAIN>` | Step 18 |
| Logs and alarms | CloudWatch Logs and Alarms; SNS create topic and subscribe | Part 3 |
| Audit | CloudTrail: create trail and event selectors (or, if the company has its own trail, ask them to add data events for your two data buckets) | Step 5 |
| Settings | SSM Parameter Store: put/get under `/hr-onboarding/*` (holds the MongoDB connection string, the Gemini key and the HR password) | Step 15 |
| Analytics | Glue and Athena: create database, tables, workgroup | Step 20 |
| Cost | Billing view and Budgets (an administrator must switch on "IAM user access to billing") | Step 2 |
| Optional | WAF, GuardDuty, Macie, Config, Backup, CloudFront | Part 7 |

Also ask the administrator these questions, and write down the answers:

- Is there already an **organization-wide CloudTrail**, **GuardDuty**, **Config**, **Security Hub**, or **budget**? If yes, skip your own copy and ask them to include your resources (this saves money and avoids duplicate alerts).
- Which **regions** are allowed? Is `ap-south-1` allowed?
- Is there a **naming or tagging rule** (for example every resource needs `CostCenter`)? Use it.
- May I create a **new VPC with a NAT gateway**, or which VPC with private subnets and a NAT gateway may I use? (MongoDB Atlas needs one fixed outbound address.)
- Is signing up for **MongoDB Atlas** (a separate company, billed separately) approved, and who pays? Or does the company already have a MongoDB service?
- Questions for your colleague (bucket `igtl-hr-analytics-ayush-2026`), see Step 10j: which account and region it is in, how it is encrypted, and whether Athena tables already exist on it.
- Who do I call if the portal is down or there is a security incident? (Fill the contact table in Part 9.)
- Who approves use of the **Gemini API** and storing Aadhaar and PAN (company policy and privacy sign-off)?

---

## Part 1. AWS words you need to know

- **Account**: the company's AWS "tenant". It holds everything and is billed monthly.
- **Region**: a physical location (Mumbai = `ap-south-1`). If something "disappeared", check the region name at the top-right of the console.
- **Console**: the website where you click. **CLI**: the `aws ...` commands. **CloudShell**: a terminal in the browser that already has the CLI.
- **IAM**: who may do what. A **user** is a person (you). A **role** is a set of permissions a service borrows. A **policy** is the written list of allowed actions.
- **S3**: file storage. A **bucket** is a top-level container. A **prefix** is a folder-like path (`onboarding/employees/IGTPL007/`).
- **MongoDB Atlas**: a hosted database. Each student's application is one **document** in a **collection** (`applications`). It is outside AWS, so containers reach it over the internet through a **NAT gateway** with one fixed address that Atlas is told to trust.
- **NAT gateway**: lets containers in a **private subnet** (no public address) make outbound calls (Atlas, Gemini) from one fixed public address, while nothing from the internet can reach them.
- **KMS**: holds the encryption key. Encrypted files are unreadable without permission to use the key.
- **Docker image / container**: the image is the packaged app; a container is a running copy of it.
- **ECR**: where the Docker image is stored.
- **ECS**: Amazon's container manager. A **cluster** is a group. A **task definition** is the recipe (image, memory, settings). A **task** is one running copy. A **service** keeps the wanted number of tasks running and restarts them if they die.
- **Fargate**: the "no servers" way to run ECS tasks. You choose CPU and memory per task and pay per second while it runs.
- **VPC / subnet / security group**: your private network, its parts, and the firewall rules around a task or load balancer.
- **ALB (Application Load Balancer)**: receives web traffic, provides HTTPS, and forwards to the right container. It is always on and billed per hour.
- **ACM**: free HTTPS certificates. **Route 53**: DNS (domain names).
- **Application Auto Scaling**: changes the number of tasks on a schedule. Used for the night shutdown.
- **Glue / Athena**: Glue is the catalogue of your tables. Athena runs SQL on the files in S3.
- **CloudWatch**: logs, graphs, alarms. **CloudTrail**: records who did what in the account. **SNS**: sends the alarm email.

### Rules that prevent most AWS disasters

- Turn on **MFA** for your user.
- Never put an AWS key, password or API key in code, `.env`, git, email, or chat. The containers use a **role**, not keys.
- Never make an S3 bucket public. HR data includes Aadhaar and PAN.
- Do not change settings you do not understand in a shared company account. Ask the administrator.

---

## Part 2. Every service you need, what it does here, and its price

**How to read:** each bullet is `Service: what it does here. Price (Mumbai, verified 2026-11-11). Required or optional.`
"Free tier" means the first amount each month that AWS does not bill. Prices are in US dollars.

### 2.1. Core services (you need all of these)

- **IAM**: your user, the container roles, MFA. **Free.** Required.
- **KMS** (encryption key): one key for secrets and logs (and for S3 too if you choose, Step 10c). **$1.00 per key per month; $0.03 per 10,000 requests; first 20,000 requests a month free.** Required.
- **MongoDB Atlas** (the database for the student records): **billed by MongoDB, not by AWS. Price not re-checked here.** The free M0 tier is only for trying (no backups). For real use choose **M10 or larger** and read the current price at https://www.mongodb.com/pricing. Atlas runs all day and night, so its cost does not stop at 12 am. Required.
- **S3** (storage): student document folders, HR tables, logs. Your own three buckets cost as below; the shared bucket's storage is billed to your colleague's account. **Storage $0.025 per GB-month (first 50 TB); PUT/COPY/POST/LIST $0.005 per 1,000 requests; GET $0.004 per 10,000 requests.** A few hundred employees is well under 1 GB. Required.
- **ECR** (image storage): stores your Docker image. **$0.10 per GB-month.** An image of about 0.4 GB with the last 5 versions kept costs about $0.20 a month. Required.
- **ECS** (container manager): cluster, task definitions, services. **No charge for ECS itself on Fargate; you pay for the Fargate resources below.** Required.
- **Fargate** (runs the containers). Billed per second while a task runs, rounded up to 1 minute:
  - **ARM (Graviton): $0.02383 per vCPU-hour and $0.00261 per GB-hour.** Recommended (cheaper).
  - **x86: $0.04256 per vCPU-hour and $0.004655 per GB-hour.** Works with a build from CloudShell.
  - **Extra disk (ephemeral storage): $0.000127 per GB-hour above the 20 GB included per task.** You need none.
  - **Your sizing** (480 hours a month each):
    - Onboarding task: 0.5 vCPU + 1 GB. ARM: $0.014525 per hour = **$6.97 a month**. x86: $0.025935 per hour = **$12.45**.
    - HR task: 0.25 vCPU + 1 GB. ARM: $0.0085675 per hour = **$4.11**. x86: $0.015295 per hour = **$7.34**.
    - Total Fargate: **ARM $11.08 a month; x86 $19.79 a month.**
  - Required.
- **Application Load Balancer** (HTTPS front door, one for both portals):
  - **$0.0239 per hour (730 hours = $17.45 a month) plus $0.008 per LCU-hour for used capacity.** At 4 to 6 uses a day the LCU part is small: about $1 to $6 a month ($5.84 if one LCU were used non-stop).
  - Required. This is the biggest fixed cost of the container design.
- **NAT gateway** (one fixed outbound address for MongoDB Atlas and Gemini; the containers sit in private subnets and have no public address): billed **per hour while it exists plus per GB processed**. **Price not re-checked here: look up the Mumbai NAT gateway rate in the Pricing Calculator.** It runs all day and night. As a rough planning figure, budget about **$40 a month** (an estimate, not a verified price). Required in this design. *(Cheaper option if the company agrees: allow `0.0.0.0/0` in Atlas and rely on the database password and TLS. This is weaker and not recommended for personal data.)*
- **Public IPv4 addresses**: the NAT gateway's Elastic IP and the load balancer (one per zone). **$0.005 per address per hour (in-use).** A load balancer in two zones plus one NAT address = 3 addresses = **$10.95 a month** (check the Elastic Load Balancing pricing page to confirm how your load balancer is charged). The Fargate tasks themselves need **no** public address in this design. Required.
- **VPC and security groups** (network and firewall): **Free.** Required.
- **ACM** (HTTPS certificate): **Free for certificates used on an ALB.** Required. *(Not re-checked on the price files: documented by AWS as free for public certificates.)*
- **Route 53** (DNS for `<DOMAIN>`): **$0.50 per hosted zone per month; $0.40 per million queries; queries to alias records pointing at an AWS load balancer are free.** Required if you use your own domain (the company DNS team may do this for you).
- **Application Auto Scaling** (the night shutdown, and adding or removing containers when CPU changes): **Free; no extra charge for Application Auto Scaling.** *(Not re-checked on the price files.)* Extra containers started by a busy period are billed as normal Fargate tasks while they run. Required.
- **SSM Parameter Store** (the MongoDB connection string, Gemini key, HR user and password as encrypted values): **Free for standard parameters.** *(Not re-checked on the price files.)* Required. Alternative: **Secrets Manager $0.40 per secret per month, $0.05 per 10,000 API calls** (three secrets = $1.20 a month).
- **Glue Data Catalog** (table definitions): **Free for the first 1 million objects and first 1 million requests; then $1 per 100,000 objects per month and $1 per million requests.** Required.
- **Athena** (SQL on the HR tables): **$5.00 per TB scanned.** Your data is megabytes, so cents a month. Required.

### 2.2. Logging, alerts and audit (set up first, Part 3)

- **CloudWatch Logs** (container logs): **$0.67 per GB ingested (Standard class); $0.03 per GB-month stored; Logs Insights queries $0.0067 per GB scanned.** A small app writes well under 1 GB a month. Required.
- **CloudWatch Alarms**: **$0.10 per standard alarm per month.** Ten alarms = $1.00 (AWS also has a monthly free tier for alarms; not re-checked). Required.
- **SNS** (alert emails): **First 1,000 email notifications a month free; then $2.00 per 100,000. First 1 million API requests free.** Required.
- **CloudTrail** (who did what in the account):
  - **Your first copy of management events: free.** Additional trails: $0.00002 per paid event ($2.00 per 100,000).
  - **S3 data events (who opened each employee file): $0.000001 per event = $1.00 per million events.** About 20,000 events a month = $0.02.
  - Required (or the company's own trail).
- **S3 logs bucket** (stores CloudTrail and S3 access logs): the same S3 storage price above, normally under $1 a month. Required.
- **AWS Budgets** (cost alerts): **first 2 budgets free** *(not re-checked)*. Required.

### 2.3. Security services (recommended; ask the administrator first, the company may already run them)

- **GuardDuty** (threat detection): **S3 data events analyzed $0.000001 per event (first 500 million a month); CloudTrail events analyzed $0.0000044 per event.** A new account gets a free trial (shown as $0.00 trial lines in the price file). About $1 to $5 a month at your volume.
- **Macie** (finds PAN and Aadhaar in the wrong place):
  - **Bucket inventory $0.0033 per bucket per day (about $0.10 per bucket a month).**
  - **Object monitoring $0.0125 per 100,000 objects a month.**
  - **Sensitive data discovery $1.25 per GB scanned (first 50,000 GB).** Scanning 1 GB once = $1.25. Run it weekly or monthly, not daily, to keep the cost down.
- **AWS Config** (checks settings against rules):
  - **$0.003 per configuration item recorded** (continuous), or **$0.012 per item for daily recording**.
  - **$0.001 per rule evaluation (first 100,000 a month).** About $3 to $10 a month.
- **IAM Access Analyzer** (warns about outside sharing): **the basic external-access analyzer is free.** *(Not re-checked.)*
- **AWS WAF** (blocks abusive requests; optional because the ALB can limit the HR portal by IP for free):
  - **$5.00 per web ACL per month; $1.00 per rule per month; $0.60 per million requests.** One ACL + 3 managed rule groups + 1 rate rule = about $9 a month plus requests. *(Read from the US pricing page; the page says prices vary by region, so check Mumbai.)*
- **AWS Backup** (scheduled backups of S3): **$0.0385 per GB-month for S3 backup storage.** For 1 GB, about $0.04 a month. Optional but recommended (S3 versioning already covers single-file mistakes).
- **CloudFront** (optional, only if you want caching or a second layer): **first 10 million HTTP/S requests a month free; then about $0.012 per 10,000 HTTPS requests in Asia.** Not needed for the ALB design.
- **Cognito** (sign-in with MFA for the HR portal): **price not re-checked.** Ask the company's SSO team first.

### 2.4. Services you do NOT need (so you do not pay for them)

- **EC2 servers**: Fargate replaces them.
- **A public IP on each task**: not used. The tasks are in private subnets (Step 12). That is why the NAT gateway above is needed.
- **App Runner**: AWS has closed it to new customers since 2026-04-30 (Appendix B), so do not choose it.
- **Interface VPC endpoints**: cost **$0.01 per GB processed** plus hourly charges per endpoint; not needed at this size. The free S3 gateway endpoint is optional.
- **Secrets Manager**: optional; SSM is free.

### 2.5. Cost summary

| Item | ARM (Graviton) | x86 |
|---|---|---|
| Fargate (2 tasks, 480 h each, no extra scaling) | $11.08 | $19.79 |
| ALB hours (730 h) | $17.45 | $17.45 |
| ALB LCU usage (estimate) | $1 to $6 | $1 to $6 |
| Public IPv4: ALB 2 zones + NAT 1 | $10.95 | $10.95 |
| KMS key | $1.00 | $1.00 |
| S3 (your 3 buckets, requests) | $1 to $2 | $1 to $2 |
| CloudWatch logs and alarms | $1 to $2 | $1 to $2 |
| ECR | $0.20 | $0.20 |
| Route 53 zone | $0.50 | $0.50 |
| CloudTrail data events, Athena, Glue, SNS, SSM | under $1 | under $1 |
| **AWS subtotal (without NAT)** | **about $45 to $53** | **about $54 to $62** |
| **NAT gateway** (price not re-checked; planning estimate) | **about $40** | **about $40** |
| **MongoDB Atlas** (billed by MongoDB; price not re-checked) | **see mongodb.com/pricing** | **see mongodb.com/pricing** |
| Add: GuardDuty, Macie, Config, Backup, WAF (optional) | +$10 to $30 | +$10 to $30 |

- The AWS part is roughly **$45 to $53 a month** on ARM (**$54 to $62** on x86), **plus the NAT gateway (estimate about $40)**, **plus MongoDB Atlas**, before the optional security extras. AWS alone (with the NAT estimate) is about **$85 to $95 a month**. Set the AWS budget (Step 2) at **$120** until you see a real bill, and track the Atlas bill separately in the Atlas console.
- The costs that do not stop at night are the **ALB, the NAT gateway, MongoDB Atlas and the public addresses**. Only the Fargate cost stops.
- If CPU gets high, ECS starts more containers (Step 19). Each extra onboarding container for a full day costs about **$0.35** on ARM (0.5 vCPU + 1 GB, $0.014525 per hour for 24 hours).
- Cheaper option if you drop MongoDB and use S3-only records (`STORAGE_BACKEND=s3`): no Atlas and no NAT gateway are needed, but concurrent-ID safety and querying are weaker. Ask me before choosing it.

---

## Part 3. Step by step: budget, alerts and logs FIRST

**Why logs first:** if something goes wrong later, logs are the only way to learn what happened. Logs that were not switched on beforehand cannot be recovered afterwards.

### Step 1. Check your sign-in (done in Part 0)

- Confirm in CloudShell: `aws sts get-caller-identity` shows your IAM user. Confirm MFA is on. **Cost: free.**

### Step 2. Set a budget alert

- Console → **Billing and Cost Management** → **Budgets** → **Create budget**. If it says you have no access, ask the administrator to enable **IAM user access to billing** or to create the budget for you.
- "Cost budget", monthly, fixed amount (suggest **$120** for this design; expected AWS cost is about $85 to $95 including the NAT gateway estimate. MongoDB Atlas is billed separately by MongoDB). Alerts at **50 %**, **80 %**, **100 %** of actual cost and **100 % forecasted**. Enter your email and the administrator's.
- Optional: Billing → **Cost Anomaly Detection** → monitor "AWS services".
- **Cost: first 2 budgets free.** If an alert email arrives, go to Part 9, problem H.

### Step 3. Create the alert channel (SNS)

- Console → **SNS** → Topics → **Create topic** → type **Standard** → name `hr-onboarding-alerts`.
- **Create subscription** → protocol **Email** → your email. Add a second person's email too.
- Open the confirmation email and click **Confirm subscription**. **No alerts arrive until you confirm.**
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

- First ask the administrator: "Is there already an organization CloudTrail?" If **yes**, ask them to add **S3 data events** for `<BUCKET>` and `<BUCKET_RESTRICTED>` (so you can see who opens each employee's file) and skip to Step 6.
- If you may create your own:
  - Console → **CloudTrail** → **Create trail**: name `hr-onboarding-trail`, storage = existing bucket `<BUCKET_LOGS>` with prefix `cloudtrail`, **log file validation: Enabled**, **CloudWatch Logs: Enabled** (new log group `/hr-onboarding/cloudtrail`), **multi-region: Yes**, events: **Management events** read and write.
  ```
  aws cloudtrail create-trail --name hr-onboarding-trail --s3-bucket-name <BUCKET_LOGS> --s3-key-prefix cloudtrail --is-multi-region-trail --enable-log-file-validation
  aws cloudtrail start-logging --name hr-onboarding-trail
  ```
- Add S3 data events once the data buckets exist (Step 10):
  ```
  aws cloudtrail put-event-selectors --trail-name hr-onboarding-trail --advanced-event-selectors '[{"Name":"Management events","FieldSelectors":[{"Field":"eventCategory","Equals":["Management"]}]},{"Name":"S3 HR data","FieldSelectors":[{"Field":"eventCategory","Equals":["Data"]},{"Field":"resources.type","Equals":["AWS::S3::Object"]},{"Field":"resources.ARN","StartsWith":["arn:aws:s3:::<BUCKET>/","arn:aws:s3:::<BUCKET_RESTRICTED>/"]}]}]'
  ```
  This command replaces the trail's event settings, so it lists the management events too. In the console check that both "Management events" and "Data events" show as enabled.
- **Cost: first management trail free; data events $0.000001 each ($1 per million). Cents at your usage.**

### Step 6. Create the CloudWatch log groups (the app's diary)

- Each container writes its output to a log group. Create them **now** with a retention period so they never grow forever:
  ```
  aws logs create-log-group --log-group-name /ecs/hr-onboarding-portal
  aws logs create-log-group --log-group-name /ecs/hr-onboarding-hr
  aws logs put-retention-policy --log-group-name /ecs/hr-onboarding-portal --retention-in-days 90
  aws logs put-retention-policy --log-group-name /ecs/hr-onboarding-hr --retention-in-days 365
  ```
- **Always set a retention period.** Without it logs are kept forever and the bill grows.
- Never log PAN, Aadhaar, bank account numbers, or passwords. Log the employee ID only. Search the code for `print(` and `log.` calls and check before going live.
- **Cost: $0.67 per GB ingested and $0.03 per GB-month stored. A small app writes well under 1 GB a month.**

### Step 7. Create the alarms (so you hear about problems)

- If you made your own CloudTrail: CloudWatch → **Logs** → `/hr-onboarding/cloudtrail` → **Metric filters** → Create. For each row, create the filter, then **Create alarm** (threshold 1, period 5 minutes, action: send to `hr-onboarding-alerts`):

| Alarm name | Filter pattern (paste in the filter box) | What it means |
|---|---|---|
| failed-logins | `{ $.eventName = "ConsoleLogin" && $.errorMessage = "Failed authentication" }` | Wrong password attempts |
| login-no-mfa | `{ $.eventName = "ConsoleLogin" && $.additionalEventData.MFAUsed != "Yes" }` | A login without MFA |
| bucket-policy-change | `{ $.eventName = "PutBucketPolicy" || $.eventName = "DeleteBucketPolicy" || $.eventName = "PutBucketPublicAccessBlock" || $.eventName = "DeleteBucketPublicAccessBlock" }` | Someone changed bucket security |
| trail-tampering | `{ $.eventName = "StopLogging" || $.eventName = "DeleteTrail" || $.eventName = "UpdateTrail" }` | Someone tried to turn off logs |
| iam-change | `{ $.eventName = "PutUserPolicy" || $.eventName = "PutRolePolicy" || $.eventName = "AttachRolePolicy" || $.eventName = "CreateAccessKey" }` | Permissions changed or key created |
| kms-disable | `{ $.eventName = "DisableKey" || $.eventName = "ScheduleKeyDeletion" }` | Encryption key at risk |

- **Container alarms** (CloudWatch → Alarms → Create alarm → Select metric), added after Step 18 when the load balancer exists:
  - **ALB `HTTPCode_Target_5XX_Count`** (per target group): Sum ≥ 5 in 5 minutes. The app is returning errors.
  - **ALB `UnHealthyHostCount`** (per target group) ≥ 1 for 5 minutes. **Do not alarm during 12 am to 8 am**: with the tasks stopped, the count of healthy hosts is 0 by design, so alarm on `UnHealthyHostCount` only (it stays 0 when no task is registered) and not on `HealthyHostCount`.
  - **ECS `CPUUtilization` and `MemoryUtilization`** (ClusterName/ServiceName) ≥ 85 % for 10 minutes: raise the task size (Part 9, problem D).
  - A **metric filter on `/ecs/hr-onboarding-portal`** for the word `ERROR` with an alarm at 5 matches per 5 minutes. The app logs a failed table write and keeps the submission, so this alarm tells you to run `python sync_tables.py`.
- **Cost: $0.10 per alarm per month.**

### Step 8. Learn to read logs now (5-minute practice)

- **Who did what in AWS:** Console → **CloudTrail** → **Event history** → filter by "User name", "Event name" (for example `DeleteObject`), or "Resource name". Shows the last 90 days of management events, free.
- **Why the app failed:** CloudWatch → Logs → Log groups → `/ecs/hr-onboarding-portal` → open the newest **log stream** (named `ecs/onboarding/<task id>`) → Ctrl+F and search `ERROR` or the employee ID.
- **Why a container stopped:** ECS → Clusters → `hr-onboarding` → service → **Tasks** tab → filter **Stopped** → open a task → **Stopped reason** and the container's **exit code** (137 = out of memory).
- **Search many lines at once:** CloudWatch → **Logs Insights** → select the log group → run:
  ```
  fields @timestamp, @message
  | filter @message like /ERROR/
  | sort @timestamp desc
  | limit 50
  ```
- **Who opened a specific employee's file** (needs data events), Logs Insights on `/hr-onboarding/cloudtrail`:
  ```
  fields @timestamp, userIdentity.arn, eventName, requestParameters.key
  | filter eventName = "GetObject" and requestParameters.key like /IGTPL007/
  | sort @timestamp desc
  ```
- **Load balancer requests:** the ALB access logs in `s3://<BUCKET_LOGS>/alb-logs/` (turned on in Step 18).
- **Detailed S3 access:** S3 server access logs in `s3://<BUCKET_LOGS>/s3-access-logs/`.
- Keep a note of these places. Part 9 refers back to them.

---

## Part 4. Step by step: encryption and storage (S3)

### Step 9. Create the encryption key (KMS)

- Console → **KMS** → Customer managed keys → **Create key**: **Symmetric**, **Encrypt and decrypt**, alias `hr-onboarding`, description "HR onboarding data". Key administrators: your IAM user. Key users: leave empty (you add the task roles in Step 14).
- Open the key → **Key rotation** → enable automatic rotation.
  ```
  aws kms create-key --description "HR onboarding data"
  aws kms create-alias --alias-name alias/hr-onboarding --target-key-id <KEY_ID>
  aws kms enable-key-rotation --key-id <KEY_ID>
  ```
- Copy the **ARN** from the key page. That is `<KMS_KEY_ARN>`. It encrypts the secrets (Step 15) and logs. It is **not** the app's `S3_KMS_KEY_ID` unless you chose that in Step 10c.
- **Warning:** if this key is deleted, all encrypted data becomes permanently unreadable. Never schedule deletion. The alarm in Step 7 watches for it.
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
- **Recommended: leave `S3_KMS_KEY_ID` empty.** The app then writes with S3's own encryption (SSE-S3, AES-256) everywhere. Skip the command below (new buckets already default to SSE-S3). Your own KMS key still protects the secrets and logs.
- Only if the company requires a KMS key on all S3 data: ask your colleague for the KMS key their bucket uses, set `S3_KMS_KEY_ID` to it, and use the same key in the command below and in Step 14.
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

**10f. Lifecycle (main bucket only).** Save as `lifecycle.json`, then `aws s3api put-bucket-lifecycle-configuration --bucket <BUCKET> --lifecycle-configuration file://lifecycle.json`:
```json
{"Rules": [
  {"ID": "old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
   "NoncurrentVersionTransitions": [{"NoncurrentDays": 30, "StorageClass": "STANDARD_IA"}],
   "NoncurrentVersionExpiration": {"NoncurrentDays": 365},
   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
  {"ID": "athena-results", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"},
   "Expiration": {"Days": 30}}
]}
```
Do not add an expiry rule on `employees/` unless your company retention policy says how long to keep records.

**10g. Access logging** (records every access in the logs bucket)
```
aws s3api put-bucket-logging --bucket <BUCKET> --bucket-logging-status '{"LoggingEnabled":{"TargetBucket":"<BUCKET_LOGS>","TargetPrefix":"s3-access-logs/main/"}}'
aws s3api put-bucket-logging --bucket <BUCKET_RESTRICTED> --bucket-logging-status '{"LoggingEnabled":{"TargetBucket":"<BUCKET_LOGS>","TargetPrefix":"s3-access-logs/restricted/"}}'
```

**10h. Go back to Step 5** and add the CloudTrail S3 data events for these two buckets.

**10i. (Optional, advanced) "Only the app can read the restricted bucket" deny rule.** Add this to the restricted bucket's policy. **Test it on a throwaway bucket first.** A wrong deny rule can lock everyone out, including you (Part 9, problem J).
```json
{"Sid": "RestrictedOnlyAppAndAdmin", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
 "Resource": ["arn:aws:s3:::<BUCKET_RESTRICTED>", "arn:aws:s3:::<BUCKET_RESTRICTED>/*"],
 "Condition": {"StringNotLike": {"aws:PrincipalArn": [
   "arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-task-role",
   "arn:aws:iam::<ACCOUNT_ID>:user/<IAM_USER>",
   "arn:aws:iam::<ACCOUNT_ID>:role/<COMPANY_ADMIN_ROLE>"]}}}
```

**10j. The shared bucket `igtl-hr-analytics-ayush-2026` (your colleague's).** You do not create or configure it. Ask your colleague these four things and write down the answers:

1. **Which AWS account owns it?** Same account as yours: only your IAM policy (Step 14) is needed. A **different** account: they must also add the bucket policy below.
2. **Which region?** `aws s3api get-bucket-location --bucket igtl-hr-analytics-ayush-2026`. Use that as `<REGION>` for everything.
3. **How is it encrypted?** SSE-S3 works as it is (leave `S3_KMS_KEY_ID` empty). If it uses their own KMS key, they must allow your task role in that key's policy, and you set `S3_KMS_KEY_ID` to it.
4. **Is "Bucket owner enforced" on** (the S3 default)? Then the files you write belong to them and their Athena can read them. Also ask: **do the Athena tables from `Data/sources_hr/athena_tables.sql` already exist on this bucket?** (Step 20 depends on it.)

If the bucket is in a **different account**, send them this to add to the bucket policy (fill in `YOUR_ACCOUNT_ID`), plus a second statement allowing `s3:ListBucket` on `arn:aws:s3:::igtl-hr-analytics-ayush-2026` for the same role:
```json
{"Sid": "HROnboardingApp", "Effect": "Allow",
 "Principal": {"AWS": "arn:aws:iam::YOUR_ACCOUNT_ID:role/hr-onboarding-task-role"},
 "Action": ["s3:GetObject", "s3:PutObject"],
 "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
              "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*",
              "arn:aws:s3:::igtl-hr-analytics-ayush-2026/onboarding/*"]}
```
The live Athena tables (database `hr_analytics`) in this bucket point at top-level folders, with **no `hr/` prefix**: `s3://igtl-hr-analytics-ayush-2026/employees/` and `.../employee_education/`. The app writes there by default (`SHARED_TABLE_PREFIX` is empty; set it to `hr/` only if you ever need the old layout).

What the app does in that bucket: it **only writes its own files**, `employees/IGTPL###.parquet` and `employee_education/IGTPL###.parquet`. It never lists, overwrites or deletes your colleague's files, and it has **no delete permission** there. **Do not** put the ID documents or the bank and sensitive tables in the shared bucket: everyone who can read it or query its Athena tables would see real PAN, Aadhaar and account numbers. In the `employees` table your Athena users will see your real students next to any sample rows (filter with `employee_id LIKE 'IGTPL%'`).

- **Cost:** storage $0.025 per GB-month, PUT $0.005 per 1,000, GET $0.004 per 10,000. A small HR dataset is about **$1 to $2 a month**.

### Step 11. What the data looks like (one folder per student, MongoDB + S3)

This is written by the code (`storage.py`, `hr_tables.py`). Do not change the key names. The student ID (for example `IGTPL007`) is issued by an **atomic counter in MongoDB**, so two containers can never give the same ID to two students. It is the student's MongoDB `_id` and the name of their S3 folder.

- **MongoDB** (database `hr_onboarding`, created automatically on the first submission)
  - `applications`: one document per student, `_id = IGTPL007`: the whole record (personal, family, education, bank, references, policies, flags, status, and `document_folder`, the S3 address of the student's folder).
  - `forms`: the form as submitted, so a flagged form can be reopened.
  - `counters`: the student-ID counter.
- **Your private documents bucket `<BUCKET>`**
  - `onboarding/employees/IGTPL007/documents/` is that student's own folder: `pan.jpg`, `aadhaar_front.jpg`, `aadhaar_back.jpg`, `photo.jpg`, `resume.pdf`, `education_certificate.pdf`, `bank_proof.jpg`, `payslip_N.pdf`, `relieving_letter.pdf`.
  - `athena-results/` holds Athena query output (auto-deleted after 30 days).
- **The shared bucket `igtl-hr-analytics-ayush-2026`** (your colleague's, you only add files)
  - `employees/IGTPL007.parquet` and `employee_education/IGTPL007.parquet` (top level, no `hr/` prefix): one small file per student per table, same columns as `Data/sources_hr/athena_tables.sql`. Written when the student submits, updated when HR approves, rejects or flags. Columns that HR fills in later (`department`, `work_email`, `manager_id` ...) are never overwritten by the app. These are the tables HR and Athena read.
- **Your restricted bucket `<BUCKET_RESTRICTED>`**
  - `hr/employee_bank_details/IGTPL007.parquet`
  - `hr/employee_sensitive_details/IGTPL007.parquet` (real PAN, Aadhaar, phone, account numbers)
- **Not filled by the app:** `employee_compensation`, `employment_details`, `leave_records`, `attendance` (the form does not collect them).
- **Logs bucket `<BUCKET_LOGS>`**: `cloudtrail/`, `s3-access-logs/`, `alb-logs/`.
- **How HR fetches:** the HR portal reads the records from MongoDB and the documents from S3, so it always shows the latest data. For reports, HR runs Athena on the tables in the shared bucket.
- **Why one folder per student:** you can grant, audit, export, or delete one person's whole record using the ID (needed for a data-deletion request: Part 10).
- Keep names, PAN, and Aadhaar **out of file names and folder names**. Use the student ID only.

---

## Part 5. Step by step: network, image, roles, settings

### Step 12. Network and firewalls (security groups)

- The containers run in **private subnets** (no public address) and reach the internet only through a **NAT gateway**. The NAT gateway has **one fixed public address**, `<NAT_IP>`, which MongoDB Atlas is told to trust (Step 21). Nothing from the internet can reach the containers.
- Create the network (VPC console → **Create VPC** → **VPC and more**): name `hr-onboarding`, **2 availability zones**, **2 public subnets** (`<PUBLIC_A>`, `<PUBLIC_B>`), **2 private subnets** (`<PRIVATE_A>`, `<PRIVATE_B>`), **NAT gateways: In 1 AZ** (one is enough and cheaper; choose "1 per AZ" only if it must survive a zone outage), **VPC endpoints: S3 Gateway** (free). Note `<VPC_ID>`. If the administrator gives you an existing VPC with private subnets and a NAT gateway, use that instead.
- Copy the NAT gateway's **Elastic IP** (VPC → NAT gateways → your gateway → Primary public IPv4 address). That is `<NAT_IP>`. It does not change while the gateway exists. **Do not delete and recreate the NAT gateway without updating Atlas** (Part 9, problem S).
- Create two **security groups** (VPC → Security groups → Create):
  - `hr-onboarding-alb-sg` (for the load balancer). Inbound: **443** from `0.0.0.0/0` and **80** from `0.0.0.0/0` (80 only redirects to 443). Outbound: default.
  - `hr-onboarding-task-sg` (for the containers). Inbound: **8000** and **8001** **from `hr-onboarding-alb-sg` only** (choose the security group as the source, not an IP range). Outbound: **443** to anywhere (ECR, S3, Gemini, CloudWatch) **and 27017** to anywhere (MongoDB Atlas speaks on port 27017; without this rule the app cannot reach the database).
  ```
  aws ec2 create-security-group --group-name hr-onboarding-alb-sg --description "ALB" --vpc-id <VPC_ID>
  aws ec2 create-security-group --group-name hr-onboarding-task-sg --description "ECS tasks" --vpc-id <VPC_ID>
  aws ec2 authorize-security-group-ingress --group-id <ALB_SG_ID> --protocol tcp --port 443 --cidr 0.0.0.0/0
  aws ec2 authorize-security-group-ingress --group-id <ALB_SG_ID> --protocol tcp --port 80 --cidr 0.0.0.0/0
  aws ec2 authorize-security-group-ingress --group-id <TASK_SG_ID> --protocol tcp --port 8000-8001 --source-group <ALB_SG_ID>
  aws ec2 authorize-security-group-egress --group-id <TASK_SG_ID> --protocol tcp --port 27017 --cidr 0.0.0.0/0
  ```
  (A new security group already allows all outbound traffic. If you replaced that default with "443 only", add the 27017 rule as shown.)
- The task security group accepts **no** traffic except from the load balancer, and the tasks have no public address.
- **Cost: security groups and the S3 gateway endpoint are free.** The NAT gateway and public IPv4 charges are in Part 2.

### Step 13. Image registry (ECR) and build

- Create the repository:
  ```
  aws ecr create-repository --repository-name hr-onboarding --image-scanning-configuration scanOnPush=true
  aws ecr put-lifecycle-policy --repository-name hr-onboarding --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep last 5","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}'
  ```
- Your existing `Dockerfile` is used as it is. Before building, check `.env` is listed in `.dockerignore`. The image must not contain secrets.
- Build and push **from your laptop with Docker Desktop** (log in with the CLI first; ask the administrator how, e.g. SSO). Use a **version tag** (`v1`, `v2`, ...), never only `latest`, so you can go back (Part 9, problem L).
  - **ARM (recommended, about 44 % cheaper compute):**
    ```
    aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
    docker buildx build --platform linux/arm64 --provenance=false -t <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/hr-onboarding:v1 --push .
    ```
    (The Windows build uses emulation and can take several minutes.)
  - **x86 (works from CloudShell too):**
    ```
    docker build --platform linux/amd64 -t <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/hr-onboarding:v1 .
    docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/hr-onboarding:v1
    ```
    In Step 16 set `cpuArchitecture` to `X86_64` instead of `ARM64`.
- **Cost: $0.10 per GB-month stored (about $0.20 for five versions). Basic scan on push is free.**

### Step 14. Roles for the containers (IAM)

- If your user cannot create roles, send this step to the administrator. The containers use **roles**. There are no access keys in the app or in `.env`.
- Two roles are needed (role names must stay exactly as written: your colleague's bucket policy in Step 10j names `hr-onboarding-task-role`):
  - **Task execution role** `hr-onboarding-exec-role` (used by ECS to start the container: pull the image, write logs, read settings).
    - IAM → Roles → Create role → trusted entity **Elastic Container Service Task**. Attach `AmazonECSTaskExecutionRolePolicy`.
    - Add an inline policy `hr-onboarding-exec`:
      ```json
      {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ssm:GetParameters", "ssm:GetParameter"],
         "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/hr-onboarding/*"},
        {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "<KMS_KEY_ARN>"}]}
      ```
  - **Task role** `hr-onboarding-task-role` (used by the app itself to reach S3). Trusted entity **Elastic Container Service Task**. Inline policy `hr-onboarding-app`:
    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {"Sid": "OwnBuckets", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": ["arn:aws:s3:::<BUCKET>/*", "arn:aws:s3:::<BUCKET_RESTRICTED>/*"]},
        {"Sid": "OwnBucketsList", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": ["arn:aws:s3:::<BUCKET>", "arn:aws:s3:::<BUCKET_RESTRICTED>"]},
        {"Sid": "SharedBucketOwnFilesOnly", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
                      "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*",
                      "arn:aws:s3:::igtl-hr-analytics-ayush-2026/onboarding/*"]},
        {"Sid": "SharedBucketList", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": "arn:aws:s3:::igtl-hr-analytics-ayush-2026",
         "Condition": {"StringLike": {"s3:prefix": ["employees/*", "employee_education/*", "onboarding/*"]}}}
      ]
    }
    ```
  - The role has **no delete permission on the shared bucket**, on purpose. If you chose a KMS key for S3 in Step 10c, also allow `kms:GenerateDataKey` and `kms:Decrypt` on that key.
  - If the shared bucket is in **another account**, this policy is only half of the permission: your colleague must also add the bucket policy from Step 10j.
- Back in **KMS**, add `hr-onboarding-exec-role` as a **key user** (it decrypts the secrets). Add `hr-onboarding-task-role` too if you use KMS for S3. Without this, secret reads or uploads fail with "AccessDenied" (Part 9, problem C).
- Better in production: a separate task role for each portal. Both write the HR tables (the onboarding portal on submit, the HR portal on approve, reject or flag), so both need the shared-bucket statements. The onboarding role needs no `DeleteObject` on your buckets for new submissions (a student reopening a flagged form replaces their own documents, so keep it unless you test without it).
- HR staff and developers get **no direct S3 access** to the restricted bucket. HR uses the portal.
- **Cost: free.**

### Step 15. Settings and secrets (SSM Parameter Store)

- Store the secrets as encrypted parameters. ECS injects them into the container as environment variables when it starts, so **no code change and nothing in the image**:
  ```
  aws ssm put-parameter --name /hr-onboarding/mongodb-uri --type SecureString --key-id <KMS_KEY_ARN> --value "<MONGODB_URI>"
  aws ssm put-parameter --name /hr-onboarding/gemini-api-key --type SecureString --key-id <KMS_KEY_ARN> --value "<YOUR_GEMINI_KEY>"
  aws ssm put-parameter --name /hr-onboarding/hr-user --type SecureString --key-id <KMS_KEY_ARN> --value "hr_admin"
  aws ssm put-parameter --name /hr-onboarding/hr-password --type SecureString --key-id <KMS_KEY_ARN> --value "<LONG_RANDOM_PASSWORD>"
  ```
- The MongoDB string comes from Step 21 (create the Atlas cluster first, then store the string here). It contains the database password, so treat it like one.
- Use a password of 20+ random characters. Change the HR password every 90 days (calendar reminder).
- Your local `.env` holds a real Gemini key. If this folder was ever shared, create a new key in Google AI Studio and delete the old one. Keep `.env` out of git and out of the image.
- Type values directly into the console or CloudShell. Never paste real secrets into chat, email, or a document.
- **Cost: free (standard parameters).** Option: Secrets Manager costs $0.40 per secret per month; in the task definition use `valueFrom` with the secret ARN instead.

---

## Part 6. Step by step: run the containers

### Step 16. Create the ECS cluster and the two task definitions

- Cluster: Console → **ECS** → Clusters → **Create cluster** → name `hr-onboarding`, infrastructure **AWS Fargate (serverless)** only. Turn off Container Insights (it costs extra).
  ```
  aws ecs create-cluster --cluster-name hr-onboarding
  ```
- **Task definition 1: the onboarding portal.** Save as `portal-task.json`:
  ```json
  {
    "family": "hr-onboarding-portal",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-exec-role",
    "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/hr-onboarding-task-role",
    "containerDefinitions": [{
      "name": "onboarding",
      "image": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/hr-onboarding:v1",
      "essential": true,
      "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
      "environment": [
        {"name": "HOST", "value": "0.0.0.0"},
        {"name": "PORT", "value": "8000"},
        {"name": "STORAGE_BACKEND", "value": "mongo_s3"},
        {"name": "MONGODB_DB", "value": "hr_onboarding"},
        {"name": "S3_BUCKET", "value": "igtl-hr-analytics-ayush-2026"},
        {"name": "S3_PREFIX", "value": "onboarding/"},
        {"name": "S3_DOCUMENTS_BUCKET", "value": "<BUCKET>"},
        {"name": "S3_RESTRICTED_BUCKET", "value": "<BUCKET_RESTRICTED>"},
        {"name": "AWS_REGION", "value": "<REGION>"},
        {"name": "GEMMA_MODEL", "value": "gemma-4-26b-a4b-it"}
      ],
      "secrets": [
        {"name": "MONGODB_URI", "valueFrom": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/hr-onboarding/mongodb-uri"},
        {"name": "GEMINI_API_KEY", "valueFrom": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/hr-onboarding/gemini-api-key"}
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1"],
        "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 30
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {"awslogs-group": "/ecs/hr-onboarding-portal", "awslogs-region": "<REGION>", "awslogs-stream-prefix": "ecs"}
      }
    }]
  }
  ```
  ```
  aws ecs register-task-definition --cli-input-json file://portal-task.json
  ```
- **Task definition 2: the HR portal.** Copy the file as `hr-task.json` and change:
  - `family` → `hr-onboarding-hr`, `cpu` → `256`, `memory` → `1024`.
  - Container `name` → `hr`, add `"command": ["python", "hr_info.py"]`, port **8001**, `PORT` → `8001`, health check URL port **8001**, log group `/ecs/hr-onboarding-hr`.
  - Remove `GEMINI_API_KEY`/`GEMMA_MODEL`. Add `{"name": "ONBOARDING_URL", "value": "https://<ONBOARDING_DOMAIN>"}` to `environment` (the HR portal puts it in the link it gives a flagged student).
  - Keep `STORAGE_BACKEND`, `MONGODB_DB`, `S3_*`, `AWS_REGION` and the `MONGODB_URI` secret **identical** to the portal's: both services must see the same database and buckets.
  - `secrets`: add `HR_USER` → `.../parameter/hr-onboarding/hr-user` and `HR_PASSWORD` → `.../parameter/hr-onboarding/hr-password`.
- If you built for x86 (Step 13), set `"cpuArchitecture": "X86_64"` in both files.
- Valid Fargate sizes: 0.25 vCPU supports 0.5, 1, 2 GB; 0.5 vCPU supports 1 to 4 GB. If the portal is slow or runs out of memory, use `1024` CPU and `2048` memory (Part 9, problem D).
- **Cost:** a task definition is free. You pay only while a task runs (Part 2).

### Step 17. Create the two ECS services (start with the load balancer first, Step 18)

- Do **Steps 18 and 21 first** (load balancer and target groups; MongoDB), then create each service so it registers with its target group. The tasks go in the **private** subnets with **no** public IP:
  ```
  aws ecs create-service --cluster hr-onboarding --service-name portal --task-definition hr-onboarding-portal --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[<PRIVATE_A>,<PRIVATE_B>],securityGroups=[<TASK_SG_ID>],assignPublicIp=DISABLED}" \
    --load-balancers targetGroupArn=<PORTAL_TG_ARN>,containerName=onboarding,containerPort=8000 \
    --health-check-grace-period-seconds 60 --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100"
  aws ecs create-service --cluster hr-onboarding --service-name hr --task-definition hr-onboarding-hr --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[<PRIVATE_A>,<PRIVATE_B>],securityGroups=[<TASK_SG_ID>],assignPublicIp=DISABLED}" \
    --load-balancers targetGroupArn=<HR_TG_ARN>,containerName=hr,containerPort=8001 \
    --health-check-grace-period-seconds 60 --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100"
  ```
- The **deployment circuit breaker with rollback** means a broken new version is automatically rolled back to the last working one.
- Check in the console: ECS → cluster → service → **Tasks** shows **Running**, and after about a minute the target group shows **healthy**.
- If the first start fails, read Part 9, problem A (task stops) before changing anything.
- **Cost:** one task each, billed per second while running (Part 2.5).

### Step 18. HTTPS front door: ALB, certificate, domain

- **Domain and certificate**
  - Ask the company IT/DNS team for two names (for example `onboard.<company>.com` and `hr.<company>.com`), or use Route 53 for your own domain.
  - **ACM** → **Request certificate** (in `<REGION>`) → public → add both names → DNS validation → add the CNAME records it shows (**Create records in Route 53** if the zone is in Route 53) → wait for **Issued**. Free.
- **Target groups** (EC2 → Target Groups → Create), two of them, target type **IP**, protocol **HTTP**, VPC `<VPC_ID>`:
  - `hr-onboarding-portal-tg`: port **8000**, health check path `/health`.
  - `hr-onboarding-hr-tg`: port **8001**, health check path `/health`.
  - Leave them empty: ECS registers the tasks.
  ```
  aws elbv2 create-target-group --name hr-onboarding-portal-tg --protocol HTTP --port 8000 --vpc-id <VPC_ID> --target-type ip --health-check-path /health
  aws elbv2 create-target-group --name hr-onboarding-hr-tg --protocol HTTP --port 8001 --vpc-id <VPC_ID> --target-type ip --health-check-path /health
  ```
- **Load balancer** (EC2 → Load Balancers → Create → Application Load Balancer): name `hr-onboarding-alb`, **internet-facing**, IPv4, VPC `<VPC_ID>`, the **two public** subnets (`<PUBLIC_A>`, `<PUBLIC_B>`), security group `hr-onboarding-alb-sg`.
  ```
  aws elbv2 create-load-balancer --name hr-onboarding-alb --subnets <PUBLIC_A> <PUBLIC_B> --security-groups <ALB_SG_ID> --scheme internet-facing --type application
  ```
- **Listeners**
  - **443 (HTTPS)**: certificate = your ACM certificate, security policy `ELBSecurityPolicy-TLS13-1-2-2021-06`. Default action: forward to `hr-onboarding-portal-tg`.
    - **Rule for the HR portal:** if **Host** is `<HR_DOMAIN>` **and Source IP** is your **office range** (for example `203.0.113.0/24`; find your public IP at https://checkip.amazonaws.com) → forward to `hr-onboarding-hr-tg`. A second rule: Host is `<HR_DOMAIN>` (any other IP) → **Return fixed response 403**. The source-IP condition is a built-in ALB feature, so the HR portal is limited to the office without WAF.
  - **80 (HTTP)**: redirect to HTTPS (port 443).
- **Load balancer settings** (Attributes → Edit):
  - **Access logs: On**, to `s3://<BUCKET_LOGS>/alb-logs/` (the console adds the needed bucket-policy statement if you let it).
  - **Drop invalid header fields: On**. **Idle timeout: 120 seconds** (document uploads and Gemini calls).
- **DNS**: Route 53 → create **A (alias)** records `<ONBOARDING_DOMAIN>` and `<HR_DOMAIN>` pointing to the load balancer (alias queries are free). Or ask the company DNS team to add CNAME records to the load balancer's DNS name.
- Now create the two ECS services (Step 17) and, after they are healthy, open both domains in a browser.
- **Cost: ALB $0.0239 per hour (about $17.45 a month) + $0.008 per LCU-hour used + public IPv4 $0.005 per address per hour. ACM free. Route 53 $0.50 per zone.**

### Step 19. The 12 am to 8 am shutdown (Application Auto Scaling)

- This step does two jobs with Application Auto Scaling:
  - **Scale with load:** during opening hours ECS adds containers when average CPU is above 60 % and removes them when it falls. Any container can serve any student because the records are in MongoDB and the files in S3, so adding or losing a container never loses a submission. The onboarding portal runs **1 to 4** containers, the HR portal **1 to 2** (raise the maximum later if you need more).
  - **Night shutdown:** setting the maximum capacity to 0 stops the tasks, which ends the Fargate charges. AWS documents scheduled actions for ECS services and a minimum capacity of 0 for scaling to zero (Appendix B).
- Register each service as a scalable target (the daytime limits):
  ```
  aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --min-capacity 1 --max-capacity 4
  aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/hr --min-capacity 1 --max-capacity 2
  ```
- Add the CPU rule to each service. Save as `scaling.json`:
  ```json
  {"TargetValue": 60.0,
   "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
   "ScaleOutCooldown": 60, "ScaleInCooldown": 180}
  ```
  ```
  aws application-autoscaling put-scaling-policy --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --policy-name cpu60 --policy-type TargetTrackingScaling --target-tracking-scaling-policy-configuration file://scaling.json
  aws application-autoscaling put-scaling-policy --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/hr --policy-name cpu60 --policy-type TargetTrackingScaling --target-tracking-scaling-policy-configuration file://scaling.json
  ```
- Create the **four** scheduled actions. The "off" action sets minimum and maximum to 0; the "on" action sets the daytime limits again. **Always give the time zone `Asia/Kolkata`**, otherwise the times are in UTC. The "on" action runs at **07:55** so the containers are ready by 8:00 (a Fargate task needs about a minute to start):
  ```
  aws application-autoscaling put-scheduled-action --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --scheduled-action-name portal-off --schedule "cron(0 0 * * ? *)" --timezone Asia/Kolkata --scalable-target-action MinCapacity=0,MaxCapacity=0
  aws application-autoscaling put-scheduled-action --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --scheduled-action-name portal-on  --schedule "cron(55 7 * * ? *)" --timezone Asia/Kolkata --scalable-target-action MinCapacity=1,MaxCapacity=4
  aws application-autoscaling put-scheduled-action --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/hr --scheduled-action-name hr-off --schedule "cron(0 0 * * ? *)" --timezone Asia/Kolkata --scalable-target-action MinCapacity=0,MaxCapacity=0
  aws application-autoscaling put-scheduled-action --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/hr --scheduled-action-name hr-on  --schedule "cron(55 7 * * ? *)" --timezone Asia/Kolkata --scalable-target-action MinCapacity=1,MaxCapacity=2
  ```
- Test scaling once: in CloudShell run a short burst of requests against `https://<ONBOARDING_DOMAIN>/health` while watching ECS → service → **Desired tasks**; or temporarily lower `TargetValue` to 10 and confirm a second task starts, then put it back to 60. Two things limit how far this scales: the MongoDB tier (raise the Atlas tier) and the Gemini API quota (document reading is the slow step).
- Check: `aws application-autoscaling describe-scheduled-actions --service-namespace ecs`.
- Test straight away: create a temporary "off" action a few minutes ahead, then open the address. At night, the load balancer has no healthy container and shows **HTTP 503**; that is the "off" state working. Delete the test action.
- **Manual switch** (maintenance or an incident), in CloudShell:
  ```
  aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --min-capacity 0 --max-capacity 0     # OFF
  aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount --resource-id service/hr-onboarding/portal --min-capacity 1 --max-capacity 4     # ON
  ```
  (Repeat with `service/hr-onboarding/hr`; its ON limits are `--min-capacity 1 --max-capacity 2`.)
- Tell employees the opening hours (8 am to midnight IST) in the invitation email.
- What does **not** stop at night: the ALB, the NAT gateway, their public addresses, MongoDB Atlas, and S3 storage. Only the Fargate charges stop. The data and records are untouched while the containers are off.
- **Cost: free.**

### Step 20. Athena and Glue (SQL on the HR tables), then load the data

- **First ask your colleague** (Step 10j, question 4): do the Athena tables from `Data/sources_hr/athena_tables.sql` already exist on `igtl-hr-analytics-ayush-2026`? If **yes**, do not create them again: your new `employees/IGTPL###.parquet` files are picked up by their existing `employees` and `employee_education` tables, and you only need permission to query them. Skip to the Test line. If **no**, create them yourself as below.
- Console → **Glue** → Databases → **Add database** `hr_analytics` (skip if it exists).
- **Athena** → Workgroups → **Create** `hr-onboarding`: result location `s3://<BUCKET>/athena-results/` (your own bucket, not the shared one), tick "Override client-side settings", per-query data limit 10 GB.
- In the query editor choose workgroup `hr-onboarding`, open `Data/sources_hr/athena_tables.sql`, replace `YOUR_BUCKET` with `igtl-hr-analytics-ayush-2026`, and run each `CREATE TABLE`. The **bank and sensitive tables must point at `s3://<BUCKET_RESTRICTED>/hr/...`** (your private bucket), not the shared one.
- Test: `SELECT employee_id, full_name, designation, employment_status FROM hr_analytics.employees WHERE employee_id LIKE 'IGTPL%';`
- Optional **Lake Formation** (ask the administrator): grant SELECT on the two restricted tables only to the HR-admin role.
- **Load the existing data** into MongoDB and S3 (from the laptop that has the project folder, because the old `submissions/`, `forms/` and `uploads/` folders are there). First add your laptop's public IP to Atlas → Network Access (temporary, Step 21), and sign in to AWS on the laptop (company SSO or CLI profile). In PowerShell:
  ```
  $env:STORAGE_BACKEND="mongo_s3"; $env:AWS_REGION="<REGION>"
  $env:S3_BUCKET="igtl-hr-analytics-ayush-2026"; $env:S3_PREFIX="onboarding/"
  $env:S3_DOCUMENTS_BUCKET="<BUCKET>"; $env:S3_RESTRICTED_BUCKET="<BUCKET_RESTRICTED>"
  $env:MONGODB_URI="<MONGODB_URI>"
  python sync_tables.py --from-local
  ```
  (Linux or CloudShell: `export NAME=value`. CloudShell's address is not in Atlas, so use the laptop.) It copies each student's record and form into MongoDB, their documents into `onboarding/employees/<id>/documents/` in your private bucket, and writes the Parquet table files. It is safe to run again. Later, `python sync_tables.py` alone rebuilds all the tables from MongoDB. Check with `aws s3 ls s3://<BUCKET>/onboarding/employees/` and in Atlas → Browse Collections → `applications`.
- After it works, remove your laptop's IP from Atlas, **delete any copy of the project zip** from CloudShell, and remove the local `uploads/` and `submissions/` folders from laptops. They contain Aadhaar and PAN.
- **Cost: Athena $5 per TB scanned (megabytes = cents); Glue free at this size.**

### Step 21. MongoDB Atlas (the student records)

Do this **before Step 15** (the connection string is a secret you store there) and before the services start.

- Create an account at https://www.mongodb.com/atlas and a cluster **in the same region as your AWS setup** (AWS, `ap-south-1`/Mumbai if available). Use at least **M10** for real use. The free M0 is only for trying: it has no backups and a small limit. Check with the company whether Atlas is approved for personal data (Part 0.4).
- **Database Access** → add a user `hr_app` with a long random password and the role **readWrite on database `hr_onboarding`** only (not "Atlas admin").
- **Network Access** → add `<NAT_IP>` (the NAT gateway's address from Step 12). **Remove "allow from anywhere".** Add your own laptop's IP only while you load data or test, then delete it.
- **Connect** → **Drivers** → copy the connection string. It looks like `mongodb+srv://hr_app:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`. Put the real password in. This is `<MONGODB_URI>`. Store it in SSM in Step 15. If the password has special characters (`@ : / ?`), use letters and numbers only to avoid encoding problems.
- **Backup** → turn on cloud backup for the cluster (not on M0). Atlas stores the student records, so this is the record backup; S3 versioning covers the files.
- The collections (`applications`, `forms`, `counters`) are created by the app on the first submission. Nothing to create by hand.
- The connection uses TLS. Never log or paste the string. If it leaks, change the database user's password in Atlas and update the parameter (Part 9, problem I).
- **Cost:** billed by MongoDB. Price not re-checked here; see https://www.mongodb.com/pricing.

---

## Part 7. Extra security services (turn on once the app works)

**Ask the administrator first**: in a company account GuardDuty, Config, Security Hub, and an organization CloudTrail are often already run centrally. Do not duplicate them. Prices are in Part 2.3.

- **GuardDuty:** Console → GuardDuty → Enable → Settings → turn on **S3 Protection**. Send "High" findings to `hr-onboarding-alerts` (EventBridge rule → SNS).
- **Macie:** Console → Macie → Enable → create a **discovery job** on `<BUCKET>` and `<BUCKET_RESTRICTED>` (run weekly or monthly, $1.25 per GB scanned). Expected: matches under `onboarding/employees/` in `<BUCKET>` (the ID documents) and in the restricted tables. **Not expected:** any match in the shared bucket's `employees/` or `employee_education/` (ask your colleague to scan it, or to run Macie there; Part 9, problem K).
- **AWS Config:** record all resources (or daily recording to save money), store in `<BUCKET_LOGS>`. Managed rules: `s3-bucket-public-read-prohibited`, `s3-bucket-ssl-requests-only`, `s3-default-encryption-kms`, `s3-bucket-versioning-enabled`, `s3-bucket-logging-enabled`, `cloudtrail-enabled`, `alb-http-to-https-redirection-check`, `restricted-ssh`. "Noncompliant" means a setting changed or was missed.
- **IAM Access Analyzer:** IAM → Access Analyzer → Create analyzer (account). Free.
- **WAF (optional):** WAF → Create web ACL (region `<REGION>`) → associate with `hr-onboarding-alb` → managed rule groups `AWSManagedRulesCommonRuleSet` and `AWSManagedRulesKnownBadInputsRuleSet` + a **rate-based rule** of 300 requests per 5 minutes per IP → turn on logging.
- **AWS Backup:** Backup → Create plan `hr-daily` (02:00, keep 35 days), resources = both data buckets.
- **HR sign-in with MFA:** ask the company SSO team about adding an "Authenticate with OIDC/Cognito" action on the ALB listener rule for the HR domain.
- **Tags** on every resource: `Project=hr-onboarding`, `Environment=prod`, `DataClass=PII`, plus any tag the company requires.

---

## Part 8. Total cost, and how to lower it

- **Expected: AWS about $45 to $53 a month on ARM ($54 to $62 on x86), plus the NAT gateway (planning estimate about $40, not re-checked), plus MongoDB Atlas (billed by MongoDB)**, before the optional extras (Part 2.5). The budget alert in Step 2 is set to $120.
- Always-on parts that you pay for even at night: **ALB $17.45 + public addresses $10.95 + LCU $1 to $6**, the **NAT gateway**, **MongoDB Atlas**, S3 storage, KMS $1.
- Parts that stop at night: **Fargate $11.08 (ARM) or $19.79 (x86)**.
- Lower it:
  - Choose **ARM** (saves about $8.70 a month).
  - Keep the maximum task counts (Step 19) low until you need more. Each extra container costs money only while it runs.
  - Shrink the HR task to `256` CPU and `512` MB if it is lightweight (HR task ARM: 0.25 vCPU + 0.5 GB = $0.0072625 per hour = $3.49 a month).
  - Keep the optional security extras off at first, or use the company's central ones.
- Costs that grow with use: MongoDB tier, NAT data processing, extra containers, S3 storage, CloudWatch log ingestion ($0.67 per GB), CloudTrail data events, Athena scans.
- **Data transfer out** to the internet is billed per GB after the free monthly amount; employee documents are small. *(Rate not re-checked.)*
- Review **Billing → Bills** (grouped by service) every week for the first month.

---

## Part 9. What if something goes wrong? (Troubleshooting and recovery)

**Golden process for any problem**

- Step 1: **Stay calm. Do not delete things or change many settings at once.** Write down the time and what you see.
- Step 2: Check **where to look** (Step 8): CloudWatch logs for app errors, ECS stopped-task reasons, CloudTrail Event history for AWS changes.
- Step 3: Find the problem below. Fix **one thing**, then test.
- Step 4: Write a short note: what happened, what fixed it. It becomes your own runbook.
- Step 5: Anything you cannot fix with your IAM user's permissions goes to the **company AWS administrator**, with the exact error text, the time, and what you tried.

### A. The website does not open (503, 502, 504, timeout) or a task keeps stopping

- **Is it between 12 am and 8 am IST?** A **503** from the load balancer is the shutdown working. Wait until 8 am or switch on manually (problem P).
- **During the day, HTTP 503:** no healthy container. ECS → cluster → service. Is **Running count** 1? If 0, the schedule or a manual change set the maximum to 0: run the ON commands in Step 19.
- **Running count is 0 and the service keeps starting then stopping:** open the service **Events** tab and the **stopped task** (Step 8). Read the **Stopped reason**:
  - `CannotPullContainerError`: the image tag does not exist, the architecture is wrong, or the task cannot reach ECR. Check the image tag in the task definition, that the **NAT gateway exists and the private subnets route `0.0.0.0/0` to it** (VPC → Route tables), and the task security group allows outbound 443.
  - `ResourceInitializationError ... unable to retrieve secrets`: the execution role cannot read the SSM parameter or decrypt it. Check the parameter name and the `hr-onboarding-exec-role` policy and KMS key-user entry (Step 14).
  - `Essential container in task exited` with exit code **1**: the app crashed at startup. Read the log stream (a missing environment variable name is the usual cause). The message `STORAGE_BACKEND=mongo_s3 needs MONGODB_URI` means the `MONGODB_URI` secret is missing from that task definition.
  - Log shows `ServerSelectionTimeoutError` or the page works but submitting hangs and fails: see problem S (MongoDB).
  - Exit code **137**: out of memory. Raise memory (problem D).
  - `exec format error` in the log: the image is for the wrong CPU. The task definition says ARM64 but you pushed x86, or the other way round.
  - `Task failed ELB health checks`: see the next point.
- **Target group shows unhealthy:** EC2 → Target groups → the group → **Targets** tab shows the reason. Check the container port (8000 or 8001), that the health path `/health` returns 200, and that `hr-onboarding-task-sg` accepts that port from `hr-onboarding-alb-sg`.
- **502 or 504 after a long wait:** the app took longer than the ALB idle timeout (120 s). Check the log for slow Gemini calls.
- **Only the HR portal cannot be reached from the office:** your office IP changed. Update the source-IP condition on the HR listener rule (Step 18).
- **Still failing:** redeploy the previous version (problem L) and tell the administrator.

### B. The browser says "certificate expired" or "not secure"

- ACM → Certificates → check the status. If **Pending validation**, the DNS validation record is missing: create it.
- ACM renews on its own only while the DNS validation record exists. Never delete that record.
- If the browser says "not secure" on the load balancer's raw address, that is expected. Use the `<DOMAIN>` names.

### C. "AccessDenied", "not authorized", or "403" when saving or reading files

- Read the full error text. It names the action (for example `s3:PutObject`) and the resource, and **who** was denied.
- **If the app was denied** (the log shows the task role): check in order:
  - `hr-onboarding-task-role` is missing the permission (Step 14).
  - The role is **not a KMS key user** (error mentions `kms:GenerateDataKey` or `kms:Decrypt`). Fix: KMS → key → Key users → add the role. If the denied bucket is `igtl-hr-analytics-ayush-2026` and it uses a KMS key, the **key policy on your colleague's side** must allow your role.
  - **The denied bucket is the shared one** (`igtl-hr-analytics-ayush-2026`): if it is in another account, your colleague has not added (or has changed) the bucket policy from Step 10j. Send them the exact error. Test from CloudShell: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/`.
  - The bucket name in `S3_BUCKET` or `S3_DOCUMENTS_BUCKET` has a typo or is in another region.
  - A bucket policy has an explicit Deny (Step 10i, or HTTPS-only if the call is HTTP).
- **If you were denied** (CloudShell says `User: arn:aws:iam::...:user/<IAM_USER> is not authorized to perform: X`): your IAM user lacks a permission. Send the **exact message** to the administrator and ask for that permission on those resources only.
- **"explicit deny in a service control policy":** the company blocks this action for everyone in the account. Only the administrator can change it.
- Find what was denied: CloudTrail → Event history → filter "Event name" = the action, look for `errorCode: AccessDenied`.

### D. Uploads fail, the app is slow, or the container runs out of memory

- Check the app log for `ERROR` lines at that time (Step 8).
- ECS → service → **Health and metrics** (or CloudWatch `MemoryUtilization`/`CPUUtilization`). If memory is near 100 % or exit code is 137: register a **new task definition revision** with bigger sizes (for the portal `1024` CPU and `2048` memory), then update the service:
  ```
  aws ecs register-task-definition --cli-input-json file://portal-task.json
  aws ecs update-service --cluster hr-onboarding --service portal --task-definition hr-onboarding-portal
  ```
  Larger size costs more (ARM 1 vCPU + 2 GB = $0.02905 per hour = $13.94 a month at 480 hours).
- Check the Gemini API: if the key is wrong or the quota is exhausted, the form checks fail. Rotate the key if needed (problem I).
- Uploads over the app limit (`MAX_UPLOAD_BYTES`, now 5 MB) are refused by the app itself.

### E. Data is missing in Athena or the HR portal

- A failed table write is logged as `HR table sync failed` **but the submission is still saved in MongoDB**. Look for `ERROR` in the onboarding log (most often an AccessDenied on the shared bucket, problem C).
- Run `python sync_tables.py` (with the environment variables from Step 20, from a laptop whose IP is allowed in Atlas) to rebuild all the tables from the records in MongoDB.
- Student missing in the **HR portal** itself: the portal reads MongoDB, not Athena. Check Atlas → `applications` for the ID, and that both services use the same `MONGODB_URI` and `MONGODB_DB` (Step 16).
- In Athena, make sure the workgroup is `hr-onboarding` and the database is `hr_analytics`. The wrong region in the console also shows empty tables.
- Athena error "HIVE_..." or "schema mismatch": re-run the table's `CREATE TABLE` from `athena_tables.sql`.
- Athena error "S3 access denied": your user needs `s3:GetObject` and `kms:Decrypt` (and Lake Formation permission). Ask the administrator (problem C).

### F. A file was deleted or overwritten by mistake

- Because versioning is on, nothing is really gone.
- Console → S3 → bucket → open the folder → toggle **Show versions**. A deleted file shows a "Delete marker". Select the **delete marker** and click **Delete** (the file returns). For an older version of a changed file, download it and upload it again.
- CLI: `aws s3api list-object-versions --bucket <BUCKET> --prefix employees/IGTPL007/` shows version IDs. Remove a delete marker with `aws s3api delete-object --bucket <BUCKET> --key <KEY> --version-id <DELETE_MARKER_ID>`.
- Find who deleted it: CloudTrail data events (`DeleteObject`) or the Logs Insights query in Step 8.
- Versions older than 365 days are removed by the lifecycle rule, so recover quickly.
- **A student record deleted or changed in MongoDB by mistake:** Atlas → your cluster → **Backup** → **Restore** (point-in-time or a snapshot) into a *new* cluster, copy the document back (or use `mongodump`/`mongorestore`), then run `python sync_tables.py` to rewrite the tables. Never restore over the live cluster without the administrator.
- **A file deleted in the shared bucket:** the app has no delete permission there, so this came from someone else. Ask your colleague to restore it from their bucket versions, or run `python sync_tables.py` to rewrite the student's table files from MongoDB.

### G. I think data was seen or taken by the wrong person (possible breach)

- **Contain first, investigate second.** Within the first hour:
  - Switch the app off: the OFF commands in Step 19 (maximum 0 for both services).
  - **Call the administrator and your manager now.** As an IAM user you cannot see or fix everything; they must disable suspect users or keys.
  - If a bucket was exposed: S3 → bucket → Permissions → make sure **Block all public access is On**.
  - If a key leaked: problem I.
- **Preserve evidence:** do not delete logs. CloudTrail, S3 access logs, ALB logs, and CloudWatch logs are your proof of what happened and when.
- **Find out what happened:** CloudTrail Event history and the Logs Insights query in Step 8. Check GuardDuty and Macie findings if enabled.
- **Tell the right people:** the HR head and the legal or compliance owner immediately. Under India's DPDP Act a personal data breach has to be reported to the Data Protection Board and to the affected people. Ask legal for the deadline and wording. Do not wait for the investigation to finish.
- **Recover:** rotate every secret (Gemini key, HR password), review all IAM changes in the last 90 days, restore anything modified from S3 versions (problem F), switch the app on again, and write down the cause and the fix.

### H. Surprise bill or a budget alert arrived

- Billing → **Cost Explorer** → group by **Service**, last 7 days. The top service is the cause. (Ask the administrator for access if you cannot see it.)
- Typical causes and fixes:
  - **Fargate** high: the schedule did not stop the tasks, or the task size is large. Check ECS Running count at night (problem P).
  - **EC2-Other / NAT gateway / Public IPv4** high: the NAT gateway is billed every hour plus per GB. A second or forgotten NAT gateway (VPC → NAT gateways, check all regions) or very large downloads through it are the usual causes. Public addresses are billed $0.005 per hour each.
  - **Elastic Load Balancing** high: a second or test load balancer was left running (EC2 → Load Balancers, and check **all regions**). Delete it.
  - **CloudWatch Logs** high: something logs too much ($0.67 per GB). Fix the app log level, set a retention policy.
  - **CloudTrail data events** high: a script is reading many files. Check who in the logs.
  - **Macie** high: a discovery job is scanning too often ($1.25 per GB). Make it weekly or monthly.
  - **Unfamiliar services or regions**: possible leaked keys, treat as problems I and G.
- Delete things you do not use.

### I. A password, API key or AWS access key was leaked (pasted in chat, committed to git, emailed)

- **Rotate right away** (deleting is not enough, assume it was copied):
  - Gemini key: create a new one in Google AI Studio, update the parameter, restart the service, delete the old key:
    ```
    aws ssm put-parameter --name /hr-onboarding/gemini-api-key --type SecureString --key-id <KMS_KEY_ARN> --value "<NEW_KEY>" --overwrite
    aws ecs update-service --cluster hr-onboarding --service portal --force-new-deployment
    ```
  - HR password: update `/hr-onboarding/hr-password` the same way and `--force-new-deployment` on service `hr`.
  - MongoDB password (or the `<MONGODB_URI>` leaked): Atlas → Database Access → edit `hr_app` → set a new password; update `/hr-onboarding/mongodb-uri` with `--overwrite`; then `--force-new-deployment` on **both** services. Check Atlas → Network Access shows only `<NAT_IP>`.
  - AWS access key or your password: tell the administrator; they deactivate it. You can also do IAM → your user → Security credentials → **Make inactive**, then delete.
- If the key was in git, rewriting history is not enough. Treat it as public forever.

### J. I locked myself out (bad bucket policy, lost MFA, permission removed)

- **Bucket policy deny locked everyone out:** you cannot fix it with an IAM user if the deny covers you. Ask the administrator or the account owner (root user) to delete or edit the bucket policy. Say which bucket and when it changed.
- **Lost MFA device:** the administrator resets it (IAM → user → Security credentials → remove the old device) and you add a new one.
- **"Access denied" on things that worked yesterday:** the administrator may have changed your permissions or a company policy. Ask.

### K. Macie or Config reports PAN or Aadhaar in the wrong place, or a bucket is no longer encrypted/blocked

- Open the finding to see the exact object key.
- Move the file to the right bucket and prefix, or delete it, and fix the code or process that put it there.
- For a Config "Noncompliant" item, re-apply the setting from Step 10 and check CloudTrail Event history to see who changed it.

### L. A new image broke the portal: roll back

- The service has a **deployment circuit breaker**, so a version that cannot start is rolled back automatically. If a version starts but behaves badly, go back by hand. Find the earlier task definition revision:
  ```
  aws ecs list-task-definitions --family-prefix hr-onboarding-portal --sort DESC
  aws ecs update-service --cluster hr-onboarding --service portal --task-definition hr-onboarding-portal:<OLD_REVISION>
  ```
  (Repeat for `hr-onboarding-hr`.) Wait about two minutes, test the site, and read the log (Step 8).
- To release a new version: push a new tag (`v2`), register a new task definition revision pointing to it, and run `update-service` with it. Test it first on a copy service (for example `portal-test` with its own target group) before changing the real one.

### M. Everything in a service is deleted or broken beyond repair

- Nothing is stored inside the containers (the records are in MongoDB and the files in S3), so you recreate them: register the task definitions again (Step 16), create the service (Step 17), and register the scalable target and the four schedules (Step 19). No data is lost. Recovery time: about 15 to 30 minutes.
- Keep `portal-task.json` and `hr-task.json` (without secrets) in a safe place; they are your recipe.

### N. A whole region or service has an outage

- Check https://health.aws.amazon.com/health/status and the **Health Dashboard** in the console.
- Nothing to fix on your side. Tell HR the portals are temporarily down. Data in S3 has 99.999999999 % durability and is stored across several data centres in the region.
- To protect against a whole-region failure, set up cross-region replication to another region (check data-residency rules first). This doubles storage cost.

### O. I deleted a whole bucket, a KMS key, or the trail

- **KMS key scheduled for deletion:** KMS → key → **Cancel key deletion** (7 to 30 day waiting period). After deletion, data encrypted with it cannot be recovered.
- **Bucket deleted:** the data is gone. Restore from **AWS Backup** or a replica if you set one up. That is why Backup is recommended (Part 7).
- **CloudTrail stopped:** CloudTrail → Trails → **Start logging**. Check who stopped it in Event history. The alarm in Step 7 should have emailed you.

### P. The schedule did not work: app is ON at night or OFF in the daytime

- `aws application-autoscaling describe-scheduled-actions --service-namespace ecs` lists the four actions with their times and time zone. Each must say `Asia/Kolkata`. `describe-scaling-activities` shows what ran and any error.
- If the scheduled actions are missing, an error mentions a missing service-linked role: ask the administrator to allow `CreateServiceLinkedRole` for Application Auto Scaling (Part 0.4).
- Use the manual switch in Step 19 to fix the state immediately, then fix the schedule.
- Check the live state: `aws ecs describe-services --cluster hr-onboarding --services portal hr --query "services[].{name:serviceName,desired:desiredCount,running:runningCount}"`.

### Q. I cannot do a step because my IAM user lacks permission

- This is normal in a company account. Do not look for tricks.
- Copy the exact message, the step number from this guide, and the permission you need (Part 0.4 has the list). Send it to the administrator and wait for the grant. Ask for everything in one go.

### R. I need to look inside a running container

- Prefer logs. If you really need a shell, the administrator must allow ECS Exec (`ecs:ExecuteCommand`, plus the SSM messages permissions on the task role) and the service must be created with `--enable-execute-command`. Do not leave it on in production unless needed.

### S. MongoDB problems: `ServerSelectionTimeoutError`, "Authentication failed", or submit hangs

- Atlas is outside AWS, so check three things in order:
  - **Atlas Network Access** must list `<NAT_IP>` (VPC → NAT gateways → the address). If the NAT gateway was deleted and recreated, the address changed: add the new one. Most common cause.
  - **The task can reach port 27017:** `hr-onboarding-task-sg` outbound must allow 27017 (Step 12) and the private subnets must route to the NAT gateway.
  - **The string and password:** `/hr-onboarding/mongodb-uri` has the right user, password and cluster name; the user has readWrite on `hr_onboarding`; the cluster is not paused (free and some tiers pause when idle).
- Atlas → **Metrics** and **Real-time** show connections arriving. No connections at all points to the network (first two bullets); "authentication failed" points to the string.
- If Atlas itself is down, check https://status.mongodb.com. The portals show errors until it is back; nothing already stored is lost.

### Who to contact in an emergency (fill this in and print it)

| Role | Name | Phone / email |
|---|---|---|
| You (AWS operator) | | |
| Backup operator | | |
| Company AWS administrator | | |
| HR head | | |
| Legal / compliance | | |
| Management contact | | |

### Routine checks that prevent emergencies

- **Every day (2 minutes):** look for alarm emails. Open both portals once after 8 am.
- **Every week:** Billing → Bills for unexpected costs. CloudWatch → ECS CPU and memory, ALB 5xx. GuardDuty and Macie findings (if enabled).
- **Every month:** check that the four schedules ran. Check that backups ran. Ask the administrator to review who has access to `hr-onboarding-*`. Rebuild the image on the latest `python` base image, push a new version tag, and roll it out (problem L).
- **Every 90 days:** change the HR password and rotate the Gemini key.
- **Every year:** test a restore (one deleted file from versions, one from Backup). Review the data-retention period. Review this guide.

---

## Part 10. Final security checklist

Tick each one before real employee data goes in.

**Storage**
- [ ] Block Public Access is on for your three buckets
- [ ] Encryption on your buckets (SSE-S3, or your agreed KMS key) and your own KMS key (rotation on) on the secrets and logs
- [ ] Bucket policy blocks plain HTTP and TLS below 1.2
- [ ] Versioning on and lifecycle rules set
- [ ] Bank and sensitive tables are **only** in `<BUCKET_RESTRICTED>`, and the ID documents are **only** in `<BUCKET>`. **Nothing but `employees` and `employee_education` rows is in `igtl-hr-analytics-ayush-2026`**
- [ ] Your colleague has confirmed who can read the shared bucket and its Athena tables (the tables hold your real students' names and contact details)
- [ ] No names, PAN or Aadhaar in S3 file or folder names

**Database (MongoDB Atlas)**
- [ ] Atlas Network Access lists only `<NAT_IP>` (no "anywhere"), the database user has readWrite on `hr_onboarding` only, cloud backup is on (M10 or larger)
- [ ] The connection string is only in SSM, never in the image, `.env` in git, chat or logs
- [ ] A test submission shows one document in `applications` and one folder in `<BUCKET>/onboarding/employees/<id>/documents/`

**Access**
- [ ] MFA on your IAM user. You never use the root user
- [ ] Containers use roles. **No access keys anywhere**
- [ ] Production: separate task roles for the onboarding and HR containers
- [ ] The task security group accepts traffic from the load balancer only
- [ ] The HR portal is HTTPS only, limited to office IPs on the listener rule, with a long password that is rotated
- [ ] Permission to see employee rows is checked in the backend. The AI model never decides it

**Availability and cost**
- [ ] The four schedules exist, use `Asia/Kolkata`, and a test showed the app off at night and on by 8 am
- [ ] The CPU scaling rule exists on both services and a test showed a second container starting under load
- [ ] Stopping one container (`aws ecs stop-task`) leaves the site up and ECS starts a replacement
- [ ] Circuit breaker with rollback enabled on both services
- [ ] Budget alert confirmed by a test email

**Logging and detection**
- [ ] CloudTrail with S3 data events on both data buckets (yours or the company's)
- [ ] S3 access logs, ALB access logs and CloudWatch container logs, retention set
- [ ] Alarms from Step 7 confirmed by a test (trigger one and check the email)
- [ ] GuardDuty, Macie, Config on, or confirmed covered by the company
- [ ] No PAN, Aadhaar, account numbers, or passwords in app logs

**Data governance (India, DPDP Act 2023)**
- [ ] Purpose and consent recorded for Aadhaar and PAN. Only what is needed is collected
- [ ] Retention period decided and enforced
- [ ] Deletion-request process: delete the student's documents `onboarding/employees/<id>/` (all versions) in `<BUCKET>`, their `hr/*/<id>.parquet` files in `<BUCKET_RESTRICTED>`, their documents `applications` and `forms` in MongoDB (`_id = <id>`), and their `employees/<id>.parquet` and `employee_education/<id>.parquet` in the shared bucket (the app role cannot delete there: ask your colleague), then verify with `list-object-versions` and in Atlas
- [ ] Contact list (Part 9) filled in
- [ ] Data stays in `ap-south-1`

---

## Part 11. Verify the finished setup

- Public access blocked: `aws s3api get-public-access-block --bucket <BUCKET>` shows four `true` values.
- Encryption on: `aws s3api get-bucket-encryption --bucket <BUCKET_RESTRICTED>` shows an encryption rule (`AES256` or `aws:kms`).
- The shared bucket is reachable by the app's role: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/` works for you (or run the app and watch for AccessDenied).
- Containers running: `aws ecs describe-services --cluster hr-onboarding --services portal hr` shows `runningCount` 1 each. Target groups show **healthy**.
- HTTPS works: `curl -I http://<ONBOARDING_DOMAIN>` returns 301; `curl -I https://<ONBOARDING_DOMAIN>/health` returns 200.
- The HR portal is limited: from outside the office range, `https://<HR_DOMAIN>` returns 403.
- A test submission (open `https://<ONBOARDING_DOMAIN>`, fill the form, submit) creates all of these:
  - a new document in Atlas → Browse Collections → `applications` with the new ID, and one in `forms`;
  - the student's own folder: `aws s3 ls s3://<BUCKET>/onboarding/employees/<ID>/documents/`;
  - the table files in the shared bucket: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/` shows `<ID>.parquet`;
  - the restricted files: `aws s3 ls s3://<BUCKET_RESTRICTED>/hr/employee_bank_details/` shows `<ID>.parquet`.
- Two students never share an ID or folder: submit two applications at the same time from two browsers; both get different IDs.
- Sensitive tables are not in the shared bucket: `aws s3 ls s3://igtl-hr-analytics-ayush-2026/employee_bank_details/` returns nothing, while the same path in `<BUCKET_RESTRICTED>` lists files.
- HR fetches it: open `https://<HR_DOMAIN>`, sign in, the student is listed with the documents. Approve it, then check `employees/<ID>.parquet` now says `Approved - Joining` (Athena: `SELECT employment_status FROM hr_analytics.employees WHERE employee_id='<ID>'`).
- The audit trail works: wait 5 to 15 minutes, then run the "who opened this file" query from Step 8.
- Athena works: `SELECT count(*) FROM hr_analytics.employees;`
- Shutdown test: run the OFF commands (Step 19), the address shows 503 within about two minutes, run the ON commands, the address works again after about a minute.
- Alarm test: stop a task (`aws ecs stop-task ...`) or force an error and confirm an alarm email arrives.
- Restore test: delete a test file, find the delete marker, restore it (problem F).
- Negative test: sign in as a user without access and confirm the restricted bucket answers AccessDenied.

---

## Part 12. Suggested order of work

- Day 1: Part 0 (sign in, MFA, CloudShell, send the request list). Wait for permissions. Steps 2 to 8 (budget, alerts, logs). **Do not skip.**
- Day 1 also: send your colleague the questions and bucket policy text in Step 10j, and start the MongoDB Atlas signup (Step 21). Both can take a day to answer.
- Day 2: Steps 9 to 11 (key, your two buckets, the shared bucket answers), 12 (network, to get `<NAT_IP>`), 21 (MongoDB), 14 and 15 (roles, secrets), 20 (load data) to see your folders in S3 and MongoDB.
- Day 3: Step 13 (image build and push), Step 16 (cluster and task definitions).
- Day 4: Step 18 (load balancer, certificate, domain), Step 17 (services), Step 19 (scaling and shutdown schedule). Test the portals, scaling and the shutdown.
- Day 5: Part 7 (extra security, with the administrator), Part 10 (checklist), Part 11 (verify). Then run the alarm, shutdown, and restore tests.

## Part 13. About `main.tf` (Terraform)

- Terraform is **not used** for this deployment. The existing `main.tf` is out of date (an older EC2 design that does not match this guide): do not run it. You can delete the file.

---

## Appendix A. Lambda container images (older S3-only variant, not the current design)

- This appendix describes an alternative for the older **S3-only** version. It does not cover MongoDB, the NAT gateway or the shared bucket, so treat it only as background. Use ECS Fargate (this guide).
- AWS Lambda can also run your Docker image (a "container image" function). You pay only when someone uses the app, so there is **no ALB, no public IP, and no always-on charge**. The night shutdown is a scheduled change of reserved concurrency.
- Trade-offs: about a 6 MB limit per request (so `MAX_UPLOAD_BYTES` would drop to 4 MB), a slower first request after idle, and a 15-minute maximum per request. It needs the free AWS Lambda Web Adapter added to the Dockerfile.
- Cost: in the earlier version of this guide it was estimated at about $3 to $6 a month in total. **Those Lambda numbers were not re-verified from the price files in this edition**, so confirm them before relying on them.
- Choose ECS Fargate (this guide) if you want standard always-on containers with no request-size limit. Choose Lambda if cost matters more. Ask me for the Lambda steps if you want to compare.

## Appendix B. Sources used for the prices and facts (retrieved 2026-11-11)

- AWS Price List files (official, Mumbai region `ap-south-1`): `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<service>/current/ap-south-1/index.json` for these services: AmazonECS (Fargate), AWSELB (Application Load Balancer), AmazonVPC (public IPv4, VPC endpoints), AmazonS3, awskms, AmazonECR, AWSSecretsManager, AmazonCloudWatch, AmazonAthena, AWSCloudTrail, AmazonGuardDuty, AmazonMacie, AWSConfig, AWSBackup, AmazonSNS, AWSGlue, AmazonRoute53 and AmazonCloudFront (global files).
- AWS Fargate pricing page: https://aws.amazon.com/fargate/pricing/
- Elastic Load Balancing pricing page: https://aws.amazon.com/elasticloadbalancing/pricing/
- AWS WAF pricing page: https://aws.amazon.com/waf/pricing/ (US rates; Mumbai not listed)
- Amazon ECS service auto scaling (scheduled actions, scaling to zero): https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html
- AWS App Runner availability change (closed to new customers from 2026-04-30; AWS recommends Amazon ECS Express Mode as the migration target): https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html
- Not re-checked on the files and marked in the text: ACM, SSM Parameter Store standard parameters, Application Auto Scaling, IAM Access Analyzer basic analyzer, AWS Budgets free allowance, CloudWatch alarm free tier, data transfer out, **NAT gateway (the $40 a month is a planning estimate)**, Cognito, **MongoDB Atlas (billed by MongoDB: https://www.mongodb.com/pricing)**.
- Facts about this app's storage (MongoDB collections, S3 keys, the `S3_*` and `MONGODB_*` settings) come from the project's own `storage.py` and `hr_tables.py`. They were tested with an in-memory MongoDB and a fake S3 client, **not against real MongoDB Atlas or AWS**. Expect to fix small account-specific things during the first real deployment.

## Appendix C. All app settings (environment variables)

| Variable | Meaning |
|---|---|
| `STORAGE_BACKEND` | `mongo_s3` (this deployment), `s3` (records as JSON in S3, no MongoDB), `local` (default, files on disk, for a laptop) |
| `MONGODB_URI`, `MONGODB_DB` | MongoDB connection string (a secret, from SSM) and database name (default `hr_onboarding`) |
| `S3_BUCKET` | the shared bucket `igtl-hr-analytics-ayush-2026`: only the `employees` and `employee_education` tables |
| `S3_PREFIX` | prefix for the per-student folders, `onboarding/`. **Required** with `STORAGE_BACKEND=s3` |
| `S3_DOCUMENTS_BUCKET` | **Required.** Your private bucket `<BUCKET>` for the uploaded documents. Must differ from `S3_BUCKET` |
| `S3_RESTRICTED_BUCKET` | **Required.** Your private bucket `<BUCKET_RESTRICTED>` for the bank and sensitive tables. Must differ from `S3_BUCKET` and `S3_DOCUMENTS_BUCKET` |
| `SHARED_TABLE_PREFIX` | folder in front of `employees/` and `employee_education/` in the shared bucket. Default empty (top level), matching the live Athena tables |

The app **refuses to start**, with an error naming the variable, if `S3_DOCUMENTS_BUCKET` or `S3_RESTRICTED_BUCKET` is missing or empty, or equals `S3_BUCKET` (or each other), or if `S3_PREFIX` is empty with `STORAGE_BACKEND=s3`.
| `S3_KMS_KEY_ID` | encrypt **all** S3 writes with this KMS key. Leave empty unless Step 10c says otherwise |
| `AWS_REGION` | region of the buckets |
| `GEMINI_API_KEY`, `GEMMA_MODEL` | document reading (onboarding portal only) |
| `HR_USER`, `HR_PASSWORD` | HR portal login (required on AWS) |
| `ONBOARDING_URL` | public address of the onboarding portal, used in the HR portal's link to a flagged student |
| `HOST`, `PORT` | listen address (`0.0.0.0` in the image) and port (8000 / 8001) |
