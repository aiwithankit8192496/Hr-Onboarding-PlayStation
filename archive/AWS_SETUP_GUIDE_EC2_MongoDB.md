# AWS Setup Guide: HR Onboarding (One Small EC2 Server, MongoDB + S3, Company IAM User)

Written for someone new to AWS who has been given a **company IAM user** (not the root account). It matches this project's code (`Dockerfile`, `docker-compose.aws.yml`, `Caddyfile`, `storage.py`, `hr_tables.py`, `sync_tables.py`, `react_onboard.py`, `hr_info.py`).

**What this version is**

- **Low cost now, scalable later.** The expected load is **7 to 9 applications a day**. The app runs as **Docker containers on one small EC2 server** (`t4g.small`) with **Caddy** in front for HTTPS. There is **no load balancer, no NAT gateway and no autoscaling**, because that load does not need them: this saves roughly $65 to $70 a month compared with the ECS Fargate design this guide used before. When a client needs high availability or many users, the **same Docker image and the same settings** move to **ECS Fargate behind a load balancer** (Appendix D, which keeps the old steps).
- Three containers: the **onboarding portal** (`python react_onboard.py`, port 8000), the **HR portal** (`python hr_info.py`, port 8001) and **Caddy** (ports 80 and 443, gets the HTTPS certificates by itself). Only Caddy is reachable from the internet. `docker-compose.aws.yml` and `Caddyfile` in the project define all three.
- **Where the data lives (`STORAGE_BACKEND=mongo_s3`):**
  - **MongoDB Atlas** holds each student's application record (one document per student, the student ID is its `_id`) and issues the student IDs from an atomic counter.
  - **S3** holds each student's uploaded documents in **their own folder** `onboarding/employees/<id>/documents/`, and the HR tables (same columns as `Data/sources_hr/athena_tables.sql`) as one Parquet file per student per table.
  - The HR tables go into the **shared analytics bucket `igtl-hr-analytics-ayush-2026`** (your colleague's bucket). The HR portal and Athena read from there. Bank and sensitive tables and the ID documents go to **your own private buckets**, not the shared one (Part 4).
- **The server is disposable.** It keeps nothing on its own disk, so if it dies you start a new one and repeat Steps 16 and 17 (about 15 minutes). No data is lost.
- The app is used **7 to 9 times a day** and must be **off from 12:00 am to 8:00 am (India time)**. A schedule stops the server at night (Step 19), so you do not pay for the server while it is off. MongoDB and the data stay on.
- **Logging and alerts come first (Part 3)**, before any employee data exists.
- When something breaks, go straight to **Part 9**. [DEPLOY_AWS.md](DEPLOY_AWS.md) is the short runbook of Steps 13 to 19 plus the security notes.

**About prices in this guide**

- Prices are **Mumbai (`ap-south-1`) prices in US dollars read from the official AWS Price List files and AWS pricing pages on 2026-11-11** (sources in Appendix B). They are exact for that date. They will change, so confirm in the AWS Pricing Calculator (https://calculator.aws) before you commit.
- Where a price could not be read from the AWS files, the line says **"not re-checked"**. Do not rely on those lines without checking. **The EC2 server and disk prices are of this kind.**
- A month is counted as 30 days. With the night schedule the server runs 16 hours a day (8 am to midnight), which is **480 hours a month**; without it, 730 hours.

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
| `<VPC_ID>`, `<PUBLIC_A>`, `<PUBLIC_B>` | `vpc-0abc...`, `subnet-0abc...` | A VPC with two public subnets in different zones (Step 12) |
| `<ELASTIC_IP>` | `13.x.x.x` | The server's fixed public address (Step 16). MongoDB Atlas is told to accept only this address (Steps 16 and 21), and your DNS names point to it (Step 18) |
| `<INSTANCE_ID>` | `i-0abc...` | The server's ID (Step 16), used by the night schedule |
| `<MONGODB_URI>` | `mongodb+srv://hr_app:...@cluster0.xxxx.mongodb.net/` | The MongoDB Atlas connection string (Step 21). A secret: never paste it in chat or git |
| `<ONBOARDING_DOMAIN>`, `<HR_DOMAIN>` | `onboard.example.com`, `hr.example.com` | Two web addresses you control (ask the company IT team for them) |

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
- You do not build or push any image from CloudShell or your laptop: the server builds it itself (Steps 13 and 17).

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
| Storage | S3: create buckets, bucket policy, encryption, versioning, lifecycle, logging, public-access-block, objects on your three buckets | The data (Steps 4, 10) |
| Shared bucket | S3 on `igtl-hr-analytics-ayush-2026`: read and write only under `employees/`, `employee_education/` and `onboarding/` for the task role. This is the **bucket owner's** (your colleague's) permission, not yours. Part 4, Step 10j has the policy text | Step 10j, 14 |
| Encryption | KMS: create key, alias, rotation, key policy | Step 9 |
| Roles | IAM: `CreateRole`, `PutRolePolicy`, `AttachRolePolicy`, `PassRole`, `CreateInstanceProfile`, `AddRoleToInstanceProfile` for roles named `hr-onboarding-*` only | The server's role (Step 14) |
| Server | EC2: launch, stop and start one instance, allocate and associate an **Elastic IP**, create a security group. Systems Manager: Session Manager (a shell without SSH). EventBridge Scheduler: create schedules that stop and start the instance | Steps 16, 17, 19 |
| Network | EC2/VPC: create a VPC with public subnets (no NAT gateway), a free S3 gateway endpoint, security groups (`hr-onboarding-*`) | Step 12 |
| HTTPS | Route 53 (or the company DNS team): create two A records for `<ONBOARDING_DOMAIN>` and `<HR_DOMAIN>`. The certificates come free from Caddy, not ACM | Step 18 |
| Logs and alarms | CloudWatch Logs and Alarms; SNS create topic and subscribe | Part 3 |
| Audit | CloudTrail: create trail and event selectors (or, if the company has its own trail, ask them to add data events for your two data buckets) | Step 5 |
| Settings | None needed now (the settings are in a protected file on the server). SSM Parameter Store under `/hr-onboarding/*` only when you scale up to ECS | Step 15, Appendix D |
| Analytics | Glue and Athena: create database, tables, workgroup | Step 20 |
| Cost | Billing view and Budgets (an administrator must switch on "IAM user access to billing") | Step 2 |
| Optional | GuardDuty, Macie, Config, Backup (WAF and CloudFront only if you later add a load balancer) | Part 7 |

Also ask the administrator these questions, and write down the answers:

- Is there already an **organization-wide CloudTrail**, **GuardDuty**, **Config**, **Security Hub**, or **budget**? If yes, skip your own copy and ask them to include your resources (this saves money and avoids duplicate alerts).
- Which **regions** are allowed? Is `ap-south-1` allowed?
- Is there a **naming or tagging rule** (for example every resource needs `CostCenter`)? Use it.
- May I create a **new VPC with public subnets** and launch **one EC2 server with an Elastic IP** in it, or which VPC may I use? (MongoDB Atlas needs one fixed address for the server.)
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
- **MongoDB Atlas**: a hosted database. Each student's application is one **document** in a **collection** (`applications`). It is outside AWS, so the server reaches it over the internet, and Atlas is told to trust only the server's **Elastic IP**.
- **Elastic IP**: a fixed public address that belongs to you. The server keeps it across stops, starts and replacements. It is billed per hour.
- **KMS**: holds the encryption key. Encrypted files are unreadable without permission to use the key.
- **Docker image / container**: the image is the packaged app; a container is a running copy of it. **Docker Compose** starts the three containers together from one file.
- **EC2**: a virtual server. **`t4g.small`** is a small ARM server with 2 GB of memory. **Session Manager** (part of Systems Manager) opens a shell on it from the console, so there is no SSH and no key file.
- **Caddy**: a small web server in front of the app. It provides HTTPS (it gets and renews the certificates by itself) and passes requests to the right container.
- **VPC / subnet / security group**: your private network, its parts, and the firewall rules around the server.
- **EventBridge Scheduler**: runs an action at a set time. Here it stops the server at midnight and starts it at 8 am.
- **ECS, Fargate, ALB, ECR, NAT gateway** (only for the scale-up in Appendix D): ECS runs containers for you in copies called **tasks**; **Fargate** is the "no servers" way; the **ALB** is a load balancer that spreads traffic over the copies; **ECR** stores the image; a **NAT gateway** gives private containers one fixed outbound address.
- **Route 53**: DNS (domain names).
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
- **EC2 server** (runs the three containers): one `t4g.small` (2 vCPU, 2 GB, ARM), billed per second while it runs. **Price not re-checked here: look up the Mumbai `t4g.small` Linux on-demand rate in the Pricing Calculator.** As a planning figure, budget about **$12 to $15 a month** for 730 hours (an estimate, not a verified price), about a third less with the night schedule. `t4g.micro` (1 GB) is too small for reading PDFs. A "t4g" server is burstable: sustained high CPU costs extra, which is a sign to scale up. Required.
- **EBS disk** (the server's disk): 20 GB gp3, encrypted. **Price not re-checked**; about **$2 a month**. It is billed even while the server is stopped. Required.
- **Public IPv4 address** (the Elastic IP): **$0.005 per address per hour = $3.65 a month**, also while the server is stopped. The server needs exactly one. Required.
- **Caddy and HTTPS certificates**: **free** (Caddy uses Let's Encrypt). You do **not** need a load balancer, ACM or a NAT gateway. Required (it is part of the compose file).
- **VPC and security groups** (network and firewall): **Free.** The S3 gateway endpoint is also free. Required.
- **Route 53** (DNS for your two names): **$0.50 per hosted zone per month; $0.40 per million queries.** Optional: the company DNS team may add the two A records instead.
- **EventBridge Scheduler** (the night stop and start): **the first 14 million invocations a month are free** *(not re-checked)*. Required for the night shutdown.
- **Systems Manager Session Manager** (a shell on the server, no SSH): **free** *(not re-checked)*. Required.
- **Settings and secrets**: kept in a protected file on the server (`/opt/hr/.env`, mode 600), so **free**. **SSM Parameter Store** (free for standard parameters) or **Secrets Manager** ($0.40 per secret per month) are only needed when you scale up to ECS (Appendix D).
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
- **AWS WAF** (blocks abusive requests; it attaches to a load balancer or CloudFront, **not to the single server**, so it only applies after the scale-up in Appendix D; Caddy limits the HR name by IP for free, Step 18):
  - **$5.00 per web ACL per month; $1.00 per rule per month; $0.60 per million requests.** One ACL + 3 managed rule groups + 1 rate rule = about $9 a month plus requests. *(Read from the US pricing page; the page says prices vary by region, so check Mumbai.)*
- **AWS Backup** (scheduled backups of S3): **$0.0385 per GB-month for S3 backup storage.** For 1 GB, about $0.04 a month. Optional but recommended (S3 versioning already covers single-file mistakes).
- **CloudFront** (optional, only if you want caching or a second layer): **first 10 million HTTP/S requests a month free; then about $0.012 per 10,000 HTTPS requests in Asia.** Not needed for this design.
- **Cognito** (sign-in with MFA for the HR portal): **price not re-checked.** Ask the company's SSO team first.

### 2.4. Services you do NOT need (so you do not pay for them)

- **Load balancer (ALB), NAT gateway, ECS/Fargate, ECR, autoscaling**: not needed at 7 to 9 applications a day. They come back in the scale-up (Appendix D). Together they would add about **$65 to $70 a month**.
- **ACM certificates**: Caddy gets free ones.
- **A private subnet**: the server is in a public subnet. Its security group allows only ports 80 and 443 (no SSH), so it is not reachable on anything else.
- **App Runner**: AWS has closed it to new customers since 2026-04-30 (Appendix B), so do not choose it.
- **Interface VPC endpoints**: cost **$0.01 per GB processed** plus hourly charges per endpoint; not needed at this size.
- **Secrets Manager**: optional; the settings file on the server is free.

### 2.5. Cost summary

| Item | Per month |
|---|---|
| EC2 `t4g.small` (price not re-checked; planning estimate) | about $12 to $15 (730 h); about $8 to $10 with the night schedule (480 h) |
| EBS disk, 20 GB gp3 (price not re-checked) | about $2 |
| Public IPv4 address (the Elastic IP) | $3.65 |
| KMS key | $1.00 |
| S3 (your 3 buckets, requests) | $1 to $2 |
| CloudWatch logs and alarms | $1 to $2 |
| Route 53 zone (skip if the company DNS team does it) | $0.50 |
| CloudTrail data events, Athena, Glue, SNS, EventBridge | under $1 |
| **AWS total** | **about $22 to $28** |
| **MongoDB Atlas** (billed by MongoDB; price not re-checked) | **see mongodb.com/pricing** (the free M0 has no backups) |
| Add: GuardDuty, Macie, Config, Backup (optional) | +$10 to $30 |

- The AWS part is roughly **$22 to $28 a month**, **plus MongoDB Atlas**, before the optional security extras. Set the AWS budget (Step 2) at **$40** until you see a real bill, and track the Atlas bill separately in the Atlas console.
- The ECS design this guide used before (Fargate, load balancer, NAT gateway) came to about **$85 to $95 a month** on AWS alone. You pay for that only when you need it (Appendix D).
- The costs that do not stop at night are the **disk, the Elastic IP, MongoDB Atlas, KMS and S3**. Only the server's compute cost stops.
- Bigger server later: a `t4g.medium` or `t4g.large` costs proportionally more; look up the rate in the Pricing Calculator.

---

## Part 3. Step by step: budget, alerts and logs FIRST

**Why logs first:** if something goes wrong later, logs are the only way to learn what happened. Logs that were not switched on beforehand cannot be recovered afterwards.

### Step 1. Check your sign-in (done in Part 0)

- Confirm in CloudShell: `aws sts get-caller-identity` shows your IAM user. Confirm MFA is on. **Cost: free.**

### Step 2. Set a budget alert

- Console → **Billing and Cost Management** → **Budgets** → **Create budget**. If it says you have no access, ask the administrator to enable **IAM user access to billing** or to create the budget for you.
- "Cost budget", monthly, fixed amount (suggest **$40** for this design; expected AWS cost is about $22 to $28 a month. MongoDB Atlas is billed separately by MongoDB). Alerts at **50 %**, **80 %**, **100 %** of actual cost and **100 % forecasted**. Enter your email and the administrator's.
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

- **Server alarms** (CloudWatch → Alarms → Create alarm → Select metric → EC2 → Per-Instance Metrics), added after Step 16 when the server exists. Send each to `hr-onboarding-alerts` and set **treat missing data as "not breaching"**, because the server is off at night:
  - **`StatusCheckFailed`** ≥ 1 for 5 minutes: the machine is unhealthy. Reboot it (Part 9, problem A).
  - **`CPUUtilization`** ≥ 80 % for 15 minutes: time to look at the scale-up (Appendix D).
  - A **metric filter on `/ecs/hr-onboarding-portal`** for the word `ERROR` with an alarm at 5 matches per 5 minutes. The app logs a failed table write and keeps the submission, so this alarm tells you to run `python sync_tables.py`. (The log group name starts with `/ecs/` only because the groups were created that way in Step 6; the containers on the server write to the same groups.)
  - Memory is not shown by default (it needs the CloudWatch agent, which costs extra). Exit code 137 or a restarting container tells you (Part 9, problem D).
- **Cost: $0.10 per alarm per month.**

### Step 8. Learn to read logs now (5-minute practice)

- **Who did what in AWS:** Console → **CloudTrail** → **Event history** → filter by "User name", "Event name" (for example `DeleteObject`), or "Resource name". Shows the last 90 days of management events, free.
- **Why the app failed:** CloudWatch → Logs → Log groups → `/ecs/hr-onboarding-portal` → open the newest **log stream** (named `onboarding-hr-instance`; the HR portal's is `hr-hr-instance`) → Ctrl+F and search `ERROR` or the employee ID.
- **Why a container stopped:** EC2 → the server → **Connect** → **Session Manager**, then `cd /opt/hr && docker compose -f docker-compose.aws.yml ps` (a container that keeps restarting shows `Restarting`) and `docker inspect --format '{{.State.ExitCode}}' <name>` (137 = out of memory). Caddy's own log: `docker compose -f docker-compose.aws.yml logs --tail 100 caddy`.
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
- **Web requests:** the app's own access log is **off on purpose**, because the employee edit link carries a secret token in the URL. Do not turn it back on.
- **Detailed S3 access:** S3 server access logs in `s3://<BUCKET_LOGS>/s3-access-logs/`.
- Keep a note of these places. Part 9 refers back to them.

---

## Part 4. Step by step: encryption and storage (S3)

### Step 9. Create the encryption key (KMS)

- Console → **KMS** → Customer managed keys → **Create key**: **Symmetric**, **Encrypt and decrypt**, alias `hr-onboarding`, description "HR onboarding data". Key administrators: your IAM user. Key users: leave empty (you add the server's role in Step 14, only if you use KMS for S3).
- Open the key → **Key rotation** → enable automatic rotation.
  ```
  aws kms create-key --description "HR onboarding data"
  aws kms create-alias --alias-name alias/hr-onboarding --target-key-id <KEY_ID>
  aws kms enable-key-rotation --key-id <KEY_ID>
  ```
- Copy the **ARN** from the key page. That is `<KMS_KEY_ARN>`. It encrypts logs and any other secrets you store (the app's own settings file is protected by the server's disk encryption, Step 15). It is **not** the app's `S3_KMS_KEY_ID` unless you chose that in Step 10c.
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
- **Logs bucket `<BUCKET_LOGS>`**: `cloudtrail/`, `s3-access-logs/` (and `alb-logs/` only after the scale-up in Appendix D).
- **How HR fetches:** the HR portal reads the records from MongoDB and the documents from S3, so it always shows the latest data. For reports, HR runs Athena on the tables in the shared bucket.
- **Why one folder per student:** you can grant, audit, export, or delete one person's whole record using the ID (needed for a data-deletion request: Part 10).
- Keep names, PAN, and Aadhaar **out of file names and folder names**. Use the student ID only.

---

## Part 5. Step by step: network, roles, settings

### Step 12. Network and firewall

- The server sits in a **public subnet** and has one fixed public address, an **Elastic IP** (`<ELASTIC_IP>`, created in Step 16). That is the address MongoDB Atlas is told to trust (Step 21) and where your DNS names point. **No NAT gateway is needed.** Nothing reaches the app except through Caddy on ports 80 and 443, and the security group allows no other inbound traffic (not even SSH).
- Create the network (VPC console → **Create VPC** → **VPC and more**): name `hr-onboarding`, **2 availability zones**, **2 public subnets** (`<PUBLIC_A>`, `<PUBLIC_B>`), **0 private subnets**, **NAT gateways: None**, **VPC endpoints: S3 Gateway** (free). Note `<VPC_ID>`. If the administrator gives you an existing VPC with a public subnet, use that instead.
- **If you already created the network with the earlier version of this step** (private subnets and a NAT gateway): delete the NAT gateway (VPC → NAT gateways → select → **Delete**; it costs about $40 a month) and then **release its Elastic IP** (EC2 → Elastic IPs → select → **Release**), because both keep billing otherwise. Keep the VPC, the subnets, the S3 endpoint and the security groups: they are free. `hr-onboarding-alb-sg` already has the right inbound rules (80 and 443 from anywhere) and can be used for the server; `hr-onboarding-task-sg` stays unused until the scale-up (Appendix D).
- Create the **security group** (VPC → Security groups → Create) `hr-onboarding-web-sg` for the server. Inbound: **443** and **80** from `0.0.0.0/0` (80 only redirects to 443 and lets Caddy get certificates). **No other inbound, no SSH.** Outbound: leave the default (all), because the server must reach MongoDB Atlas (port 27017), Gemini, S3 and CloudWatch.
  ```
  aws ec2 create-security-group --group-name hr-onboarding-web-sg --description "HR onboarding server" --vpc-id <VPC_ID>
  aws ec2 authorize-security-group-ingress --group-id <WEB_SG_ID> --protocol tcp --port 443 --cidr 0.0.0.0/0
  aws ec2 authorize-security-group-ingress --group-id <WEB_SG_ID> --protocol tcp --port 80 --cidr 0.0.0.0/0
  ```
- **Cost: security groups and the S3 gateway endpoint are free.** The Elastic IP is in Part 2.

### Step 13. The image: built on the server, no registry needed

- You do **not** create an image registry (ECR) or build on your laptop. The server builds the image itself from the project folder in Step 17, with the existing `Dockerfile`. Because the server is ARM (`t4g`), the image is ARM too, with no cross-building.
- Check before you copy the project: `.env` is listed in `.dockerignore` (the image must not contain secrets), and the `Dockerfile` copies every module the portals import, including `theme.py`. The container stops at start if one is missing.
- ECR and version-tagged images are only needed for the scale-up (Appendix D).
- **Cost: nothing.**

### Step 14. The role for the server (IAM)

- If your user cannot create roles, send this step to the administrator. The server uses a **role**. There are no access keys in the app, in `.env` or on the server.
- One role is needed. **Keep the name exactly `hr-onboarding-task-role`**: your colleague's bucket policy in Step 10j and the optional deny rule in Step 10i both name it.
  - IAM → Roles → Create role → trusted entity **EC2** (the console also makes an instance profile of the same name). Attach the AWS-managed policy **`AmazonSSMManagedInstanceCore`** (so you can open a shell without SSH). Add an inline policy `hr-onboarding-app`:
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
         "Condition": {"StringLike": {"s3:prefix": ["employees/*", "employee_education/*", "onboarding/*"]}}},
        {"Sid": "ContainerLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": ["arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/ecs/hr-onboarding-portal:*",
                      "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/ecs/hr-onboarding-hr:*"]}
      ]
    }
    ```
  - The role has **no delete permission on the shared bucket**, on purpose. With `mongo_s3` the app writes nothing under `onboarding/` in the shared bucket, so that prefix may be left out of the two shared-bucket statements (and of the bucket policy you send your colleague). If you chose a KMS key for S3 in Step 10c, also allow `kms:GenerateDataKey` and `kms:Decrypt` on that key.
  - If the shared bucket is in **another account**, this policy is only half of the permission: your colleague must also add the bucket policy from Step 10j.
- If you use a KMS key for S3, add `hr-onboarding-task-role` as a **key user** in KMS. Without it uploads fail with "AccessDenied" (Part 9, problem C).
- HR staff and developers get **no direct S3 access** to the restricted bucket. HR uses the portal.
- **Cost: free.**

### Step 15. Settings and secrets (a protected file on the server)

- All settings and secrets live in **one file on the server, `/opt/hr/.env`**, created in Step 17 and protected with `chmod 600` (only its owner can read it). It is **not in git and not in the image**. With one server there is nothing to inject, so no SSM or Secrets Manager is needed now (Appendix D uses SSM once you move to ECS).
- The file looks like this (the MongoDB string comes from Step 21, so create the Atlas cluster first):
  ```
  ONBOARD_DOMAIN=<ONBOARDING_DOMAIN>
  HR_DOMAIN=<HR_DOMAIN>
  ONBOARDING_URL=https://<ONBOARDING_DOMAIN>

  STORAGE_BACKEND=mongo_s3
  AWS_REGION=<REGION>
  S3_BUCKET=igtl-hr-analytics-ayush-2026
  S3_PREFIX=onboarding/
  S3_DOCUMENTS_BUCKET=<BUCKET>
  S3_RESTRICTED_BUCKET=<BUCKET_RESTRICTED>
  MONGODB_URI=<MONGODB_URI>
  MONGODB_DB=hr_onboarding

  GEMINI_API_KEY=<YOUR_GEMINI_KEY>
  GEMMA_MODEL=gemma-4-26b-a4b-it

  HR_USER=hr_admin
  HR_PASSWORD=<LONG_RANDOM_PASSWORD>
  ```
- Leave `S3_KMS_KEY_ID` out unless Step 10c says otherwise. The full list of settings is in Appendix C.
- Use a password of 20+ random characters. Change the HR password every 90 days (calendar reminder). **The HR portal refuses to start** if `HR_USER` or `HR_PASSWORD` is missing, or the password is shorter than 12 characters (with any backend except `local`); before this change it started with no login at all.
- If the password in `MONGODB_URI` has special characters (`@ : / ?`), use letters and numbers only to avoid encoding problems.
- Your local `.env` holds a real Gemini key. If this folder was ever shared, create a new key in Google AI Studio and delete the old one. Keep `.env` out of git and out of the image.
- Type values directly on the server (Session Manager, `nano`). Never paste real secrets into chat, email, or a document.
- **Cost: free.**

---

## Part 6. Step by step: run the app

### Step 16. Launch the server

- EC2 console → **Launch instance**:
  - Name `hr-onboarding`, AMI **Amazon Linux 2023** (64-bit **Arm**), instance type **`t4g.small`**.
  - **No key pair** (you use Session Manager).
  - Network: your VPC from Step 12, a **public** subnet (`<PUBLIC_A>`), **auto-assign public IP: off** (you attach the Elastic IP next). Security group `hr-onboarding-web-sg`.
  - Storage **20 GiB gp3**, **encrypted**.
  - *Advanced details* → IAM instance profile `hr-onboarding-task-role`; **Metadata version: V2 only** (IMDSv2 required).
- EC2 → **Elastic IPs** → **Allocate**, then **Associate** it with the instance. That address is `<ELASTIC_IP>`. Note the instance ID too (`<INSTANCE_ID>`).
- **MongoDB Atlas → Network Access:** add `<ELASTIC_IP>/32` and remove every other entry (Step 21). **Do not release the Elastic IP** while the app is in use: a new address would need a new Atlas entry (Part 9, problem S).
- Tag the instance and disk: `Project=hr-onboarding`, `Environment=prod`, `DataClass=PII`.
- Alarms: add the two server alarms from Step 7 now that the instance exists.
- **Cost:** the server, the disk and the Elastic IP, as in Part 2.5.

### Step 17. Put the app on the server and start it

- EC2 console → select the server → **Connect** → **Session Manager** → Connect. Then:
  ```
  sudo dnf install -y docker git
  sudo systemctl enable --now docker
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -SL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64 -o /usr/local/lib/docker/cli-plugins/docker-compose
  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  sudo mkdir -p /opt/hr && sudo chown ssm-user /opt/hr
  git clone https://github.com/aiwithankit8192496/Hr-Onboarding-PlayStation.git /opt/hr
  ```
  If the repository is private, clone with a read-only token or deploy key; never put the token in a file in the repo. A warning about `buildx` can be ignored.
- Create the settings file from Step 15: `nano /opt/hr/.env`, then `chmod 600 /opt/hr/.env`.
- Start it (the first build takes a few minutes):
  ```
  cd /opt/hr
  docker compose -f docker-compose.aws.yml up -d --build
  docker compose -f docker-compose.aws.yml ps
  ```
- A container that stops straight away with `Unsafe storage configuration` or `HR_USER and HR_PASSWORD are not both set` has a missing or wrong setting in `.env`; the error names the variable and never prints a value (Part 9, problem A).
- The containers restart by themselves after a crash or a reboot (Docker starts at boot and the services use `restart: unless-stopped`).
- **Nightly backup of MongoDB** (needed on the free Atlas tier, harmless on others). Run on the server:
  ```
  sudo tee /etc/cron.d/hr-backup >/dev/null <<'EOF'
  30 23 * * * root . /opt/hr/.env; docker run --rm mongo:7 mongodump --uri="$MONGODB_URI" --db hr_onboarding --archive --gzip | aws s3 cp - s3://<BUCKET>/backups/mongo-$(date +\%F).archive.gz
  EOF
  ```
  It runs at 11:30 pm server time (UTC unless you change it), so with the night schedule (Step 19) check the server is on at that time. Add an S3 lifecycle rule on `backups/` in `<BUCKET>` that expires objects after 30 days, restrict who can read that folder (it holds every record), and test one restore (`mongorestore --archive --gzip`, same image) into a scratch database.
- **Releasing a new version:** `cd /opt/hr && git pull && docker compose -f docker-compose.aws.yml up -d --build`. The portals are down for a few seconds, so do it outside working hours. To go back, `git checkout <previous commit>` and run the same command (Part 9, problem L).
- **Cost: nothing extra.**

### Step 18. HTTPS names and who can reach what

- **Domain:** ask the company IT/DNS team for two names (for example `onboard.<company>.com` and `hr.<company>.com`), or use Route 53 for your own domain. Create two **A records**, `<ONBOARDING_DOMAIN>` and `<HR_DOMAIN>`, both pointing to `<ELASTIC_IP>`. Do this **before** the first start, or Caddy cannot get its certificates.
- **Certificates:** nothing to do. Caddy gets them from Let's Encrypt on the first request and renews them. They live in a Docker volume, so they survive restarts. Open `https://<ONBOARDING_DOMAIN>` and `https://<HR_DOMAIN>` to test.
- **Browser protection:** the `Caddyfile` already adds HTTPS-only (HSTS), no content sniffing, no framing, no referrer and `no-store` (so ID documents are not kept in the browser cache).
- **Limit the HR name to your office (recommended).** Change the HR block in `/opt/hr/Caddyfile` to:
  ```
  {$HR_DOMAIN} {
  	import security
  	@office remote_ip 203.0.113.0/24
  	handle @office {
  		reverse_proxy hr:8001
  	}
  	respond 403
  }
  ```
  (your office or VPN range instead of `203.0.113.0/24`; find your public address at https://checkip.amazonaws.com), then `docker compose -f docker-compose.aws.yml restart caddy`. The HR portal also has its own login, which cannot be turned off.
- **What stock Caddy does not do:** rate limiting. The onboarding page is public and its AI-extraction calls are open, so set a spending cap on the Gemini key, and add a rate limit (Part 7, or a load balancer with WAF after the scale-up) before a launch with many users.
- **Cost:** Route 53 $0.50 per zone if you use it; certificates free.

### Step 19. The 12 am to 8 am shutdown (EventBridge Scheduler)

- The night shutdown **stops the server**, which ends the compute charge. The disk, the Elastic IP, MongoDB and S3 keep running and are untouched; the data is safe while the server is off.
- EventBridge → **Scheduler** → **Create schedule**, twice. **Always choose the time zone `Asia/Kolkata`**, otherwise the times are in UTC:

  | Name | Schedule | Target |
  |---|---|---|
  | `hr-stop` | recurring, cron `0 0 * * ? *` | **All APIs → Amazon EC2 → StopInstances**, input `{"InstanceIds":["<INSTANCE_ID>"]}` |
  | `hr-start` | recurring, cron `55 7 * * ? *` | **Amazon EC2 → StartInstances**, same input |

  The start runs at **07:55** so the portals are ready by 8:00 (the server needs a few minutes to boot and the containers start by themselves). Let the console create the execution role for each schedule.
- Test straight away: create a temporary stop a few minutes ahead, confirm the server stops and the address does not answer, then start it from the console and confirm both portals come back by themselves. Delete the test schedule.
- **Manual switch** (maintenance or an incident), from CloudShell: `aws ec2 stop-instances --instance-ids <INSTANCE_ID>` (OFF) and `aws ec2 start-instances --instance-ids <INSTANCE_ID>` (ON). To switch off only the app, keep the server and run `docker compose -f docker-compose.aws.yml down` on it (and `up -d` to switch on).
- Tell employees the opening hours (8 am to midnight IST) in the invitation email.
- What does **not** stop at night: the disk, the Elastic IP, MongoDB Atlas, KMS and S3 storage. Only the server's compute charge stops.
- **Cost: free** (first 14 million invocations a month, not re-checked).


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
  (Linux or CloudShell: `export NAME=value`. CloudShell's address is not in Atlas, so use the laptop. The server's own Elastic IP is already allowed, so you can also run it on the server from `/opt/hr`.) It copies each student's record and form into MongoDB, their documents into `onboarding/employees/<id>/documents/` in your private bucket, and writes the Parquet table files. It is safe to run again. Later, `python sync_tables.py` alone rebuilds all the tables from MongoDB. Check with `aws s3 ls s3://<BUCKET>/onboarding/employees/` and in Atlas → Browse Collections → `applications`.
- After it works, remove your laptop's IP from Atlas, **delete any copy of the project zip** from CloudShell, and remove the local `uploads/` and `submissions/` folders from laptops. They contain Aadhaar and PAN.
- **Cost: Athena $5 per TB scanned (megabytes = cents); Glue free at this size.**

### Step 21. MongoDB Atlas (the student records)

Do this **before Steps 15 and 17** (the connection string goes into the server's `.env`) and before the containers start.

- Create an account at https://www.mongodb.com/atlas and a cluster **in the same region as your AWS setup** (AWS, `ap-south-1`/Mumbai if available). At 7 to 9 applications a day the size does not matter, but **backups do**. The free **M0** has no backups: use it only with the nightly dump from Step 17, or pick a small paid tier that includes backups (read the current tiers and prices at https://www.mongodb.com/pricing; not re-checked here). Check with the company whether Atlas is approved for personal data (Part 0.4).
- **Database Access** → add a user `hr_app` with a long random password and the role **readWrite on database `hr_onboarding`** only (not "Atlas admin").
- **Network Access** → add `<ELASTIC_IP>/32` (the server's address from Step 16). **Remove "allow from anywhere".** Add your own laptop's IP only while you load data or test, then delete it.
- **Connect** → **Drivers** → copy the connection string. It looks like `mongodb+srv://hr_app:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`. Put the real password in. This is `<MONGODB_URI>`. Put it in the server's `.env` in Step 15. If the password has special characters (`@ : / ?`), use letters and numbers only to avoid encoding problems.
- **Backup** → turn on cloud backup for the cluster (not available on M0; there the nightly dump from Step 17 is your backup). Atlas stores the student records, so this is the record backup; S3 versioning covers the files.
- The collections (`applications`, `forms`, `counters`) are created by the app on the first submission. Nothing to create by hand.
- The connection uses TLS. Never log or paste the string. If it leaks, change the database user's password in Atlas and update `MONGODB_URI` in the server's `.env` (Part 9, problem I).
- **Cost:** billed by MongoDB. Price not re-checked here; see https://www.mongodb.com/pricing.

---

## Part 7. Extra security services (turn on once the app works)

**Ask the administrator first**: in a company account GuardDuty, Config, Security Hub, and an organization CloudTrail are often already run centrally. Do not duplicate them. Prices are in Part 2.3.

- **GuardDuty:** Console → GuardDuty → Enable → Settings → turn on **S3 Protection**. Send "High" findings to `hr-onboarding-alerts` (EventBridge rule → SNS).
- **Macie:** Console → Macie → Enable → create a **discovery job** on `<BUCKET>` and `<BUCKET_RESTRICTED>` (run weekly or monthly, $1.25 per GB scanned). Expected: matches under `onboarding/employees/` in `<BUCKET>` (the ID documents) and in the restricted tables. **Not expected:** any match in the shared bucket's `employees/` or `employee_education/` (ask your colleague to scan it, or to run Macie there; Part 9, problem K).
- **AWS Config:** record all resources (or daily recording to save money), store in `<BUCKET_LOGS>`. Managed rules: `s3-bucket-public-read-prohibited`, `s3-bucket-ssl-requests-only`, `s3-default-encryption-kms`, `s3-bucket-versioning-enabled`, `s3-bucket-logging-enabled`, `cloudtrail-enabled`, `restricted-ssh` (and `alb-http-to-https-redirection-check` after the scale-up). "Noncompliant" means a setting changed or was missed.
- **IAM Access Analyzer:** IAM → Access Analyzer → Create analyzer (account). Free.
- **WAF (only after the scale-up, Appendix D):** WAF attaches to a load balancer or CloudFront, not to the single server. Then: WAF → Create web ACL (region `<REGION>`) → associate with the load balancer → managed rule groups `AWSManagedRulesCommonRuleSet` and `AWSManagedRulesKnownBadInputsRuleSet` + a **rate-based rule** of 300 requests per 5 minutes per IP → turn on logging. Until then, use the Gemini spending cap and the HR office restriction (Step 18).
- **AWS Backup:** Backup → Create plan `hr-daily` (02:00, keep 35 days), resources = both data buckets.
- **HR sign-in with MFA / per-person logins:** the HR portal has one shared password. Ask the company SSO team about an "Authenticate with OIDC/Cognito" action on a load balancer rule for the HR domain (after the scale-up), or plan per-person logins in the app.
- **Tags** on every resource: `Project=hr-onboarding`, `Environment=prod`, `DataClass=PII`, plus any tag the company requires.

---

## Part 8. Total cost, and how to lower it

- **Expected: AWS about $22 to $28 a month, plus MongoDB Atlas (billed by MongoDB)**, before the optional extras (Part 2.5). The EC2 and disk figures are planning estimates (not re-checked). The budget alert in Step 2 is set to **$40**.
- Always-on parts that you pay for even at night: **the disk, the Elastic IP**, **MongoDB Atlas**, S3 storage, KMS $1.
- Part that stops at night: **the server's compute**, about a third of its cost.
- Lower it:
  - Keep the one `t4g.small`. Do not add servers, a load balancer or a NAT gateway until a measurement says so (Appendix D).
  - Keep the optional security extras off at first, or use the company's central ones.
  - Release anything you stopped using: a second Elastic IP or a forgotten test server keeps billing.
- Costs that grow with use: MongoDB tier, a bigger server, S3 storage, CloudWatch log ingestion ($0.67 per GB), CloudTrail data events, Athena scans, the Gemini API.
- **Data transfer out** to the internet is billed per GB after the free monthly amount; employee documents are small. *(Rate not re-checked.)*
- Review **Billing → Bills** (grouped by service) every week for the first month.

---

## Part 9. What if something goes wrong? (Troubleshooting and recovery)

**Golden process for any problem**

- Step 1: **Stay calm. Do not delete things or change many settings at once.** Write down the time and what you see.
- Step 2: Check **where to look** (Step 8): CloudWatch logs for app errors, the container status on the server, CloudTrail Event history for AWS changes.
- Step 3: Find the problem below. Fix **one thing**, then test.
- Step 4: Write a short note: what happened, what fixed it. It becomes your own runbook.
- Step 5: Anything you cannot fix with your IAM user's permissions goes to the **company AWS administrator**, with the exact error text, the time, and what you tried.

### A. The website does not open (timeout, 502, certificate error) or a container keeps stopping

- **Is it between 12 am and 8 am IST?** The server is stopped on purpose, so the address does not answer. Wait until 8 am or start it (problem P).
- **During the day, no answer at all:** EC2 console → is the server **running** with **2/2 status checks**? If it is stopped, start it; if a status check failed, **Reboot** it. Then check:
  - EC2 → **Elastic IPs**: the address is still **associated** with the server.
  - The DNS names still point to `<ELASTIC_IP>` (`nslookup <ONBOARDING_DOMAIN>`).
  - `hr-onboarding-web-sg` allows **80 and 443** from anywhere.
- **The page opens but shows "502 Bad Gateway":** Caddy is up but a portal container is down or restarting. Open Session Manager (Step 8) and run:
  ```
  cd /opt/hr
  docker compose -f docker-compose.aws.yml ps
  docker compose -f docker-compose.aws.yml logs --tail 100 onboarding
  ```
  (use `hr` for the HR portal). The CloudWatch log groups hold the same lines. The usual causes:
  - `Unsafe storage configuration, refusing to start`: a bucket setting in `.env` is missing or the buckets are not separate. The message names the variable (Appendix C).
  - `HR_USER and HR_PASSWORD are not both set` or `HR_PASSWORD is shorter than 12`: fix `/opt/hr/.env` (Step 15), then `docker compose -f docker-compose.aws.yml up -d`.
  - `STORAGE_BACKEND=mongo_s3 needs MONGODB_URI`: the line is missing from `.env`.
  - `ServerSelectionTimeoutError`, or the page works but submitting hangs: problem S (MongoDB).
  - Exit code **137**: out of memory (problem D).
  - `ModuleNotFoundError`: the `Dockerfile` does not copy a module the app imports (the `COPY` line); rebuild after fixing it.
  - `exec format error`: the image was built for the wrong CPU. Build on the server itself (Step 17), not on another machine.
- **Only the HR portal cannot be reached from the office:** your office address changed. Update the `remote_ip` range in `/opt/hr/Caddyfile` (Step 18) and restart Caddy.
- **Still failing:** go back to the previous version (problem L) and tell the administrator.

### B. The browser says "certificate expired" or "not secure"

- Caddy gets and renews the certificates by itself, but only if the DNS names point at `<ELASTIC_IP>` and ports **80 and 443** are open to the internet (`hr-onboarding-web-sg`). Check `docker compose -f docker-compose.aws.yml logs --tail 100 caddy` for the reason.
- If the DNS record was wrong at the first start, fix it and run `docker compose -f docker-compose.aws.yml restart caddy`. Do not retry in a tight loop: Let's Encrypt pauses a name for about an hour after repeated failures.
- The certificates live in the `caddy_data` Docker volume. Never run `docker compose down -v` (it deletes the volume and forces new certificates).
- If the browser says "not secure" on the server's raw IP address, that is expected. Use the names.

### C. "AccessDenied", "not authorized", or "403" when saving or reading files

- Read the full error text. It names the action (for example `s3:PutObject`) and the resource, and **who** was denied.
- **If the app was denied** (the log shows the server's role): check in order:
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
- On the server (Session Manager): `docker stats --no-stream` shows each container's memory against its limit (900 MB for the onboarding portal, 500 MB for the HR portal, set in `docker-compose.aws.yml`). A container near its limit, or exit code 137, means it ran out of memory. Reading PDFs is the heavy part, and it runs one PDF at a time inside the container (a lock keeps the PDF library from crashing the process).
- **Fix:** move to a bigger server. Stop the instance, EC2 → Actions → Instance settings → **Change instance type** (`t4g.medium`: 4 GB), start it, and raise the onboarding `mem_limit` in `docker-compose.aws.yml` (for example `1800m`), then `docker compose -f docker-compose.aws.yml up -d`. The Elastic IP and all data stay. A bigger server costs proportionally more (Pricing Calculator). If it stays slow with many simultaneous users, that is the time for the scale-up (Appendix D).
- Check the Gemini API: if the key is wrong or the quota is exhausted, the form checks fail. Rotate the key if needed (problem I).
- Uploads over the app limit (`MAX_UPLOAD_BYTES`, now 5 MB) are refused by the app itself.

### E. Data is missing in Athena or the HR portal

- A failed table write is logged as `HR table sync failed` **but the submission is still saved in MongoDB**. Look for `ERROR` in the onboarding log (most often an AccessDenied on the shared bucket, problem C).
- Run `python sync_tables.py` (with the environment variables from Step 20, from a laptop whose IP is allowed in Atlas) to rebuild all the tables from the records in MongoDB.
- Student missing in the **HR portal** itself: the portal reads MongoDB, not Athena. Check Atlas → `applications` for the ID, and that both services use the same `MONGODB_URI` and `MONGODB_DB` (Step 15).
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
  - Switch the app off: stop the server (`aws ec2 stop-instances --instance-ids <INSTANCE_ID>`, Step 19) or run `docker compose -f docker-compose.aws.yml down` on it.
  - **Call the administrator and your manager now.** As an IAM user you cannot see or fix everything; they must disable suspect users or keys.
  - If a bucket was exposed: S3 → bucket → Permissions → make sure **Block all public access is On**.
  - If a key leaked: problem I.
- **Preserve evidence:** do not delete logs. CloudTrail, S3 access logs and CloudWatch logs are your proof of what happened and when.
- **Find out what happened:** CloudTrail Event history and the Logs Insights query in Step 8. Check GuardDuty and Macie findings if enabled.
- **Tell the right people:** the HR head and the legal or compliance owner immediately. Under India's DPDP Act a personal data breach has to be reported to the Data Protection Board and to the affected people. Ask legal for the deadline and wording. Do not wait for the investigation to finish.
- **Recover:** rotate every secret (Gemini key, HR password, MongoDB password), review all IAM changes in the last 90 days, restore anything modified from S3 versions (problem F), switch the app on again, and write down the cause and the fix.

### H. Surprise bill or a budget alert arrived

- Billing → **Cost Explorer** → group by **Service**, last 7 days. The top service is the cause. (Ask the administrator for access if you cannot see it.)
- Typical causes and fixes:
  - **EC2-Instances / EC2-Other / Public IPv4** high: a second or forgotten test server, an **Elastic IP that is allocated but not attached** (still billed at $0.005 per hour), extra disks or snapshots, or a NAT gateway left over from the earlier version of Step 12 (about $40 a month). Check EC2 → Instances, Elastic IPs and VPC → NAT gateways in **all regions** and delete what you do not use.
  - **CloudWatch Logs** high: something logs too much ($0.67 per GB). Fix the app log level, set a retention policy.
  - **CloudTrail data events** high: a script is reading many files. Check who in the logs.
  - **Macie** high: a discovery job is scanning too often ($1.25 per GB). Make it weekly or monthly.
  - **Unfamiliar services or regions**: possible leaked keys, treat as problems I and G.
- Delete things you do not use.

### I. A password, API key or AWS access key was leaked (pasted in chat, committed to git, emailed)

- **Rotate right away** (deleting is not enough, assume it was copied). On the server (Session Manager), edit `/opt/hr/.env`, then restart with `docker compose -f docker-compose.aws.yml up -d`:
  - Gemini key: create a new one in Google AI Studio, put it in `GEMINI_API_KEY`, restart, delete the old key.
  - HR password: change `HR_PASSWORD` the same way.
  - MongoDB password (or the `<MONGODB_URI>` leaked): Atlas → Database Access → edit `hr_app` → set a new password; update `MONGODB_URI` in `.env`; restart. Check Atlas → Network Access shows only `<ELASTIC_IP>`.
  - AWS access key or your password: tell the administrator; they deactivate it. You can also do IAM → your user → Security credentials → **Make inactive**, then delete. (The server itself has no access keys: it uses its role.)
- If the key was in git, rewriting history is not enough. Treat it as public forever.

### J. I locked myself out (bad bucket policy, lost MFA, permission removed)

- **Bucket policy deny locked everyone out:** you cannot fix it with an IAM user if the deny covers you. Ask the administrator or the account owner (root user) to delete or edit the bucket policy. Say which bucket and when it changed.
- **Lost MFA device:** the administrator resets it (IAM → user → Security credentials → remove the old device) and you add a new one.
- **"Access denied" on things that worked yesterday:** the administrator may have changed your permissions or a company policy. Ask.

### K. Macie or Config reports PAN or Aadhaar in the wrong place, or a bucket is no longer encrypted/blocked

- Open the finding to see the exact object key.
- Move the file to the right bucket and prefix, or delete it, and fix the code or process that put it there.
- For a Config "Noncompliant" item, re-apply the setting from Step 10 and check CloudTrail Event history to see who changed it.

### L. A new version broke the portal: roll back

- On the server: `cd /opt/hr`, `git log --oneline` to find the previous good commit, `git checkout <COMMIT>`, then `docker compose -f docker-compose.aws.yml up -d --build`. Wait about two minutes, test the site, and read the log (Step 8).
- To release a new version: `git checkout main && git pull`, then the same `up -d --build`. Do it outside working hours (the portals are down for a few seconds). Test locally first, because there is no second server to try it on.

### M. The server is deleted or broken beyond repair

- Nothing is stored on the server (the records are in MongoDB and the files in S3), so you replace it: launch a new one (Step 16), **Associate the same Elastic IP** with it (EC2 → Elastic IPs → Associate), and repeat Step 17. Atlas and DNS need no change because the address is the same. Recovery time: about 15 to 30 minutes. Caddy asks for new certificates automatically.
- The one thing that exists only on the server is **`/opt/hr/.env`**. Keep a copy of it in your company's password manager or secret store (never in git, chat or email), so you can recreate it.

### N. A whole region or service has an outage

- Check https://health.aws.amazon.com/health/status and the **Health Dashboard** in the console.
- Nothing to fix on your side. Tell HR the portals are temporarily down. Data in S3 has 99.999999999 % durability and is stored across several data centres in the region.
- To protect against a whole-region failure, set up cross-region replication to another region (check data-residency rules first). This doubles storage cost.

### O. I deleted a whole bucket, a KMS key, or the trail

- **KMS key scheduled for deletion:** KMS → key → **Cancel key deletion** (7 to 30 day waiting period). After deletion, data encrypted with it cannot be recovered.
- **Bucket deleted:** the data is gone. Restore from **AWS Backup** or a replica if you set one up. That is why Backup is recommended (Part 7).
- **CloudTrail stopped:** CloudTrail → Trails → **Start logging**. Check who stopped it in Event history. The alarm in Step 7 should have emailed you.

### P. The schedule did not work: the app is ON at night or OFF in the daytime

- EventBridge → **Scheduler** → Schedules: both `hr-stop` and `hr-start` must exist, be **Enabled**, and say the time zone `Asia/Kolkata`. The schedule's **Target** and the role it uses must allow `ec2:StopInstances` and `ec2:StartInstances` on `<INSTANCE_ID>`; a failed invocation shows in the schedule's monitoring.
- Fix the state immediately with the manual switch in Step 19 (`aws ec2 start-instances` or `stop-instances`), then fix the schedule.
- Check the live state: `aws ec2 describe-instances --instance-ids <INSTANCE_ID> --query "Reservations[].Instances[].State.Name"`.
- After a start, the containers need a few minutes to come up by themselves. If they do not, open Session Manager and run `docker compose -f docker-compose.aws.yml up -d` (and check `systemctl status docker`).

### Q. I cannot do a step because my IAM user lacks permission

- This is normal in a company account. Do not look for tricks.
- Copy the exact message, the step number from this guide, and the permission you need (Part 0.4 has the list). Send it to the administrator and wait for the grant. Ask for everything in one go.

### R. I need to look inside a running container

- EC2 console → the server → **Connect** → **Session Manager**. Prefer logs. If you really need a shell inside a container: `docker exec -it <container> sh`. Leave nothing changed, because the next `up -d --build` replaces the container.

### S. MongoDB problems: `ServerSelectionTimeoutError`, "Authentication failed", or submit hangs

- Atlas is outside AWS, so check three things in order:
  - **Atlas Network Access** must list `<ELASTIC_IP>/32`. If the Elastic IP was released and a new one allocated, the address changed: add the new one. Most common cause.
  - **The server can reach Atlas:** the security group's outbound rule must still allow all (or at least port 27017), and the instance must be in a public subnet with an internet route (VPC → Route tables → `0.0.0.0/0` to the internet gateway).
  - **The string and password:** `MONGODB_URI` in `/opt/hr/.env` has the right user, password and cluster name; the user has readWrite on `hr_onboarding`; the cluster is not paused (free and some tiers pause when idle).
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
- **Every week:** Billing → Bills for unexpected costs. CloudWatch → the server's CPU, and `docker stats --no-stream` for memory. GuardDuty and Macie findings (if enabled).
- **Every month:** check that both schedules ran. Check that the nightly MongoDB dump arrived in `<BUCKET>/backups/`. Ask the administrator to review who has access to `hr-onboarding-*`. On the server run `sudo dnf upgrade -y` and reboot it, then `cd /opt/hr && git pull && docker compose -f docker-compose.aws.yml up -d --build` to pick up the latest base image (problem L).
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
- [ ] Atlas Network Access lists only `<ELASTIC_IP>` (no "anywhere"), the database user has readWrite on `hr_onboarding` only, and there is a backup: Atlas cloud backup (paid tier) or the nightly dump to `<BUCKET>/backups/` (tested once)
- [ ] The connection string is only in `/opt/hr/.env` on the server (mode 600), never in the image, git, chat or logs, and a copy of that file is in the company's secret store
- [ ] A test submission shows one document in `applications` and one folder in `<BUCKET>/onboarding/employees/<id>/documents/`

**Access**
- [ ] MFA on your IAM user. You never use the root user
- [ ] Containers use roles. **No access keys anywhere**
- [ ] The server's security group allows only ports 80 and 443 from the internet (no SSH), and the server uses IMDSv2 only
- [ ] Production with several clients: a separate role for each portal (needs the scale-up, Appendix D)
- [ ] The HR portal is HTTPS only, limited to office IPs in the `Caddyfile` (Step 18), with a long password that is rotated
- [ ] Open the HR address without a login in a private window: it must ask for a password (the app also refuses to start with no `HR_USER`/`HR_PASSWORD`)
- [ ] The HR login is one shared password, so the portal records no per-person trail. Decide whether that is acceptable, or add per-user logins before a client uses it
- [ ] The employee edit link carries a secret token in the URL: the app's web access log is off (do not turn it back on), and no proxy or load balancer log you add may record query strings (ALB access logs do: restrict that bucket, or leave them off)
- [ ] Permission to see employee rows is checked in the backend. The AI model never decides it
- [ ] Your Gemini key is on a **paid** plan whose terms say prompts are not used to improve Google's products (the Aadhaar, PAN and payslip images are sent to it); set a spending cap on the key
- [ ] The onboarding page is public and its AI-extraction calls are open: add rate limiting (a WAF rate rule, Part 7) before a launch with many users

**Availability and cost**
- [ ] The two schedules exist, use `Asia/Kolkata`, and a test showed the app off at night and on by 8 am
- [ ] A reboot of the server brings both portals back by themselves
- [ ] `/opt/hr/.env` is backed up in the company's secret store, and you have rehearsed replacing the server (problem M) or at least read it
- [ ] Server alarms (status check, CPU) confirmed by a test email
- [ ] Budget alert confirmed by a test email

**Logging and detection**
- [ ] CloudTrail with S3 data events on both data buckets (yours or the company's)
- [ ] S3 access logs and CloudWatch container logs, retention set (the app's web access log stays off)
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
- Containers running: in Session Manager, `docker compose -f docker-compose.aws.yml ps` shows `onboarding`, `hr` and `caddy` all `Up` (the first two `healthy`).
- HTTPS works: `curl -I http://<ONBOARDING_DOMAIN>` redirects to HTTPS (308 from Caddy); `curl -I https://<ONBOARDING_DOMAIN>/health` returns 200 with the `Strict-Transport-Security` header.
- The HR portal is limited: from outside the office range, `https://<HR_DOMAIN>` returns 403; from inside, it asks for the HR login (a private window must never show the portal without a password).
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
- Shutdown test: stop the server (Step 19), the address stops answering; start it, and both portals work again by themselves after a few minutes.
- Reboot test: reboot the server from the console; both portals come back without any command.
- Alarm test: force an error (or stop the server for 5 minutes with the status alarm on) and confirm an alarm email arrives.
- Restore test: delete a test file, find the delete marker, restore it (problem F).
- Negative test: sign in as a user without access and confirm the restricted bucket answers AccessDenied.

---

## Part 12. Suggested order of work

- Day 1: Part 0 (sign in, MFA, CloudShell, send the request list). Wait for permissions. Steps 2 to 8 (budget, alerts, logs). **Do not skip.**
- Day 1 also: send your colleague the questions and bucket policy text in Step 10j, and start the MongoDB Atlas signup (Step 21). Both can take a day to answer.
- Day 2: Steps 9 to 11 (key, your buckets, the shared bucket answers), 12 (network), 21 (MongoDB), 14 and 15 (role, settings file), 20 (load data) to see your folders in S3 and MongoDB.
- Day 3: Step 16 (launch the server, Elastic IP, Atlas allowlist), Step 17 (put the app on it and start it), Step 18 (DNS names, HTTPS, HR office restriction). Test the portals.
- Day 4: Step 19 (night schedule), the server alarms (Step 7), Part 7 (extra security, with the administrator), Part 10 (checklist), Part 11 (verify). Then run the alarm, shutdown, reboot and restore tests.
- Later: Appendix D, only when a measurement or a client needs it.

## Part 13. About `main.tf` (Terraform)

- Terraform is **not used** for this deployment. The existing `main.tf` describes an older design (a different layout, with the tables under `hr/`) and must **not** be run: it would create resources that clash with the shared bucket and the existing `hr_analytics` Glue database.
- If you later set up many clients, writing a Terraform or CloudFormation version of Steps 12 to 19 is the way to make each deployment repeatable. That is a new file, not this one.

---

## Appendix A. Lambda container images (older S3-only variant, not the current design)

- This appendix describes an alternative for the older **S3-only** version. It does not cover MongoDB, the Elastic IP or the shared bucket, so treat it only as background. Use the EC2 server (this guide).
- AWS Lambda can also run your Docker image (a "container image" function). You pay only when someone uses the app, so there is **no ALB, no public IP, and no always-on charge**. The night shutdown is a scheduled change of reserved concurrency.
- Trade-offs: about a 6 MB limit per request (so `MAX_UPLOAD_BYTES` would drop to 4 MB), a slower first request after idle, and a 15-minute maximum per request. It needs the free AWS Lambda Web Adapter added to the Dockerfile.
- Cost: in the earlier version of this guide it was estimated at about $3 to $6 a month in total. **Those Lambda numbers were not re-verified from the price files in this edition**, so confirm them before relying on them.
- Choose the EC2 server (this guide) if you want standard always-on containers with no request-size limit. Choose Lambda if cost matters more. Ask me for the Lambda steps if you want to compare.

## Appendix B. Sources used for the prices and facts (retrieved 2026-11-11)

- AWS Price List files (official, Mumbai region `ap-south-1`): `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<service>/current/ap-south-1/index.json` for these services: AmazonECS (Fargate), AWSELB (Application Load Balancer), AmazonVPC (public IPv4, VPC endpoints), AmazonS3, awskms, AmazonECR, AWSSecretsManager, AmazonCloudWatch, AmazonAthena, AWSCloudTrail, AmazonGuardDuty, AmazonMacie, AWSConfig, AWSBackup, AmazonSNS, AWSGlue, AmazonRoute53 and AmazonCloudFront (global files).
- AWS Fargate pricing page: https://aws.amazon.com/fargate/pricing/
- Elastic Load Balancing pricing page: https://aws.amazon.com/elasticloadbalancing/pricing/
- AWS WAF pricing page: https://aws.amazon.com/waf/pricing/ (US rates; Mumbai not listed)
- Amazon ECS service auto scaling (scheduled actions, scaling to zero): https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html
- AWS App Runner availability change (closed to new customers from 2026-04-30; AWS recommends Amazon ECS Express Mode as the migration target): https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html
- Not re-checked on the files and marked in the text: ACM, SSM Parameter Store standard parameters, Application Auto Scaling, IAM Access Analyzer basic analyzer, AWS Budgets free allowance, CloudWatch alarm free tier, data transfer out, **EC2 `t4g.small` and the EBS disk (the $12 to $15 and $2 a month are planning estimates)**, **NAT gateway (the $40 a month, Appendix D, is a planning estimate)**, EventBridge Scheduler free allowance, Cognito, **MongoDB Atlas (billed by MongoDB: https://www.mongodb.com/pricing)**.
- Facts about this app's storage (MongoDB collections, S3 keys, the `S3_*` and `MONGODB_*` settings) come from the project's own `storage.py` and `hr_tables.py`. They were tested with an in-memory MongoDB and a fake S3 client, **not against real MongoDB Atlas or AWS**. Expect to fix small account-specific things during the first real deployment.

## Appendix C. All app settings (environment variables)

| Variable | Meaning |
|---|---|
| `ONBOARD_DOMAIN`, `HR_DOMAIN` | the two public names (used by Caddy, in `docker-compose.aws.yml`) |
| `STORAGE_BACKEND` | `mongo_s3` (this deployment), `s3` (records as JSON in `S3_BUCKET`: **not for the shared bucket**, because each whole record, with PAN and Aadhaar, would be stored there), `local` (default, files on disk, for a laptop) |
| `MONGODB_URI`, `MONGODB_DB` | MongoDB connection string (a secret, in `/opt/hr/.env`) and database name (default `hr_onboarding`) |
| `S3_BUCKET` | the shared bucket `igtl-hr-analytics-ayush-2026`: only the `employees` and `employee_education` tables |
| `S3_PREFIX` | prefix for the per-student folders, `onboarding/`. **Required** with `STORAGE_BACKEND=s3` |
| `S3_DOCUMENTS_BUCKET` | **Required.** Your private bucket `<BUCKET>` for the uploaded documents. Must differ from `S3_BUCKET` |
| `S3_RESTRICTED_BUCKET` | **Required.** Your private bucket `<BUCKET_RESTRICTED>` for the bank and sensitive tables. Must differ from `S3_BUCKET` and `S3_DOCUMENTS_BUCKET` |
| `SHARED_TABLE_PREFIX` | folder in front of `employees/` and `employee_education/` in the shared bucket. Default empty (top level), matching the live Athena tables |
| `S3_KMS_KEY_ID` | encrypt **all** S3 writes with this KMS key. Leave empty unless Step 10c says otherwise |
| `AWS_REGION` | region of the buckets (also used by the log driver) |
| `GEMINI_API_KEY`, `GEMMA_MODEL` | document reading (onboarding portal only) |
| `HR_USER`, `HR_PASSWORD` | HR portal login. **Required**: the HR portal refuses to start without both, or with a password under 12 characters, unless `STORAGE_BACKEND=local` |
| `ONBOARDING_URL` | public address of the onboarding portal, used in the HR portal's link to a flagged student |
| `HOST`, `PORT` | listen address (`0.0.0.0` in the image) and port (8000 / 8001, set by the compose file) |

The app **refuses to start**, with an error naming the variable, if `S3_DOCUMENTS_BUCKET` or `S3_RESTRICTED_BUCKET` is missing or empty, or equals `S3_BUCKET` (or each other), if `S3_PREFIX` is empty with `STORAGE_BACKEND=s3`, or if the HR portal has no login.


---

## Appendix D. Scale-up: ECS Fargate behind a load balancer (the earlier design)

Use this **only when a measurement or a client requirement says one server is no longer enough**: the client needs no downtime if a server fails, many people submit at once, or the server's CPU or memory alarm keeps firing even on a bigger server. It is the design this guide used before, kept here complete.

**It uses the same image, the same settings and the same data**, so nothing in the app or in MongoDB and S3 changes. Climb in this order and stop as soon as the problem is solved:

1. **Bigger server:** change the instance type (Part 9, problem D). Minutes, no other change.
2. **Bigger database:** raise the Atlas tier.
3. **ECS Fargate behind a load balancer:** the steps below. You get two or more copies, automatic replacement and autoscaling.

What changes compared with the one-server design:

- The containers run in **private subnets** with no public address, so MongoDB Atlas can no longer be told to trust the server's Elastic IP. You need a **NAT gateway** with its own fixed address, `<NAT_IP>`, and Atlas trusts that instead (D1). The NAT gateway is the largest single cost.
- The **load balancer** (ALB) takes over from Caddy: HTTPS with an ACM certificate, host rules, the HR office restriction. WAF and Cognito can now attach to it (Part 7).
- The image goes in **ECR** and the task definitions replace `docker-compose.aws.yml`. Secrets move from `/opt/hr/.env` to **SSM Parameter Store** (D3b). The `hr-onboarding-task-role` gets a second trusted service (ECS tasks), and a separate task execution role is added (D3).
- The two schedules are replaced by Application Auto Scaling scheduled actions (D7), and the server is stopped and deleted (release its Elastic IP).
- Cost: about **$85 to $95 a month** on AWS alone (D8), before MongoDB.

Extra placeholders used below: `<NAT_IP>` (the NAT gateway's address), `<PRIVATE_A>`, `<PRIVATE_B>` (private subnets), `<ALB_SG_ID>`, `<TASK_SG_ID>` (security groups), `<PORTAL_TG_ARN>`, `<HR_TG_ARN>` (target groups). The old numbered steps are renamed D1 to D7; "Step 20" and "Step 21" still mean the steps in Part 6.

### D1. Network for ECS: private subnets, NAT gateway, security groups

- The containers run in **private subnets** (no public address) and reach the internet only through a **NAT gateway**. The NAT gateway has **one fixed public address**, `<NAT_IP>`, which MongoDB Atlas is told to trust (Step 21). Nothing from the internet can reach the containers.
- Create the network (VPC console → **Create VPC** → **VPC and more**): name `hr-onboarding`, **2 availability zones**, **2 public subnets** (`<PUBLIC_A>`, `<PUBLIC_B>`), **2 private subnets** (`<PRIVATE_A>`, `<PRIVATE_B>`), **NAT gateways: In 1 AZ** (one is enough and cheaper; choose "1 per AZ" only if it must survive a zone outage), **VPC endpoints: S3 Gateway** (free). Note `<VPC_ID>`. If the administrator gives you an existing VPC with private subnets and a NAT gateway, use that instead.
- Copy the NAT gateway's **Elastic IP** (VPC → NAT gateways → your gateway → Primary public IPv4 address). That is `<NAT_IP>`. It does not change while the gateway exists. **Do not delete and recreate the NAT gateway without updating Atlas** (D9-S).
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

### D2. Image registry (ECR) and build

- Create the repository:
  ```
  aws ecr create-repository --repository-name hr-onboarding --image-scanning-configuration scanOnPush=true
  aws ecr put-lifecycle-policy --repository-name hr-onboarding --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep last 5","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}'
  ```
- Your existing `Dockerfile` is used as it is. Before building, check `.env` is listed in `.dockerignore`. The image must not contain secrets.
- Build and push **from your laptop with Docker Desktop** (log in with the CLI first; ask the administrator how, e.g. SSO). Use a **version tag** (`v1`, `v2`, ...), never only `latest`, so you can go back (D9-L).
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
    In D4 set `cpuArchitecture` to `X86_64` instead of `ARM64`.
- **Cost: $0.10 per GB-month stored (about $0.20 for five versions). Basic scan on push is free.**

### D3. Roles for ECS: task role and task execution role

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

### D3b. Settings and secrets in SSM Parameter Store

- Store the secrets as encrypted parameters. ECS injects them into the container as environment variables when it starts, so **no code change and nothing in the image**:
  ```
  aws ssm put-parameter --name /hr-onboarding/mongodb-uri --type SecureString --key-id <KMS_KEY_ARN> --value "<MONGODB_URI>"
  aws ssm put-parameter --name /hr-onboarding/gemini-api-key --type SecureString --key-id <KMS_KEY_ARN> --value "<YOUR_GEMINI_KEY>"
  aws ssm put-parameter --name /hr-onboarding/hr-user --type SecureString --key-id <KMS_KEY_ARN> --value "hr_admin"
  aws ssm put-parameter --name /hr-onboarding/hr-password --type SecureString --key-id <KMS_KEY_ARN> --value "<LONG_RANDOM_PASSWORD>"
  ```
- The MongoDB string comes from Step 21 (create the Atlas cluster first, then store the string here). It contains the database password, so treat it like one.
- Use a password of 20+ random characters. Change the HR password every 90 days (calendar reminder). **The HR portal now refuses to start** if `HR_USER` or `HR_PASSWORD` is missing, or the password is shorter than 12 characters (with any backend except `local`); before this change it started with no login at all.
- Your local `.env` holds a real Gemini key. If this folder was ever shared, create a new key in Google AI Studio and delete the old one. Keep `.env` out of git and out of the image.
- Type values directly into the console or CloudShell. Never paste real secrets into chat, email, or a document.
- **Cost: free (standard parameters).** Option: Secrets Manager costs $0.40 per secret per month; in the task definition use `valueFrom` with the secret ARN instead.

### D4. ECS cluster and task definitions

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
- If you built for x86 (D2), set `"cpuArchitecture": "X86_64"` in both files.
- Valid Fargate sizes: 0.25 vCPU supports 0.5, 1, 2 GB; 0.5 vCPU supports 1 to 4 GB. If the portal is slow or runs out of memory, use `1024` CPU and `2048` memory (Part 9, problem D).
- **Cost:** a task definition is free. You pay only while a task runs (Part 2).

### D5. ECS services

- Do **D6 and Step 21 first** (load balancer and target groups; MongoDB), then create each service so it registers with its target group. The tasks go in the **private** subnets with **no** public IP:
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
- If the first start fails, read D9-A (task stops) before changing anything.
- **Cost:** one task each, billed per second while running (D8).

### D6. HTTPS front door: ALB, certificate, domain

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
- Now create the two ECS services (D5) and, after they are healthy, open both domains in a browser.
- **Cost: ALB $0.0239 per hour (about $17.45 a month) + $0.008 per LCU-hour used + public IPv4 $0.005 per address per hour. ACM free. Route 53 $0.50 per zone.**

### D7. Autoscaling and the 12 am to 8 am shutdown

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

### D8. Cost of this design (the earlier cost summary)

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
- If CPU gets high, ECS starts more containers (D7). Each extra onboarding container for a full day costs about **$0.35** on ARM (0.5 vCPU + 1 GB, $0.014525 per hour for 24 hours).
- Cheaper option if you drop MongoDB and use S3-only records (`STORAGE_BACKEND=s3`): no Atlas and no NAT gateway are needed, but concurrent-ID safety and querying are weaker. Ask me before choosing it.

### D9-A. ECS: the website does not open or a task keeps stopping

- **Is it between 12 am and 8 am IST?** A **503** from the load balancer is the shutdown working. Wait until 8 am or switch on manually (D9-P).
- **During the day, HTTP 503:** no healthy container. ECS → cluster → service. Is **Running count** 1? If 0, the schedule or a manual change set the maximum to 0: run the ON commands in D7.
- **Running count is 0 and the service keeps starting then stopping:** open the service **Events** tab and the **stopped task** (Step 8). Read the **Stopped reason**:
  - `CannotPullContainerError`: the image tag does not exist, the architecture is wrong, or the task cannot reach ECR. Check the image tag in the task definition, that the **NAT gateway exists and the private subnets route `0.0.0.0/0` to it** (VPC → Route tables), and the task security group allows outbound 443.
  - `ResourceInitializationError ... unable to retrieve secrets`: the execution role cannot read the SSM parameter or decrypt it. Check the parameter name and the `hr-onboarding-exec-role` policy and KMS key-user entry (D3).
  - `Essential container in task exited` with exit code **1**: the app crashed at startup. Read the log stream (a missing environment variable name is the usual cause). The message `STORAGE_BACKEND=mongo_s3 needs MONGODB_URI` means the `MONGODB_URI` secret is missing from that task definition.
  - Log shows `ServerSelectionTimeoutError` or the page works but submitting hangs and fails: see D9-S (MongoDB).
  - Exit code **137**: out of memory. Raise memory (problem D).
  - `exec format error` in the log: the image is for the wrong CPU. The task definition says ARM64 but you pushed x86, or the other way round.
  - `Task failed ELB health checks`: see the next point.
- **Target group shows unhealthy:** EC2 → Target groups → the group → **Targets** tab shows the reason. Check the container port (8000 or 8001), that the health path `/health` returns 200, and that `hr-onboarding-task-sg` accepts that port from `hr-onboarding-alb-sg`.
- **502 or 504 after a long wait:** the app took longer than the ALB idle timeout (120 s). Check the log for slow Gemini calls.
- **Only the HR portal cannot be reached from the office:** your office IP changed. Update the source-IP condition on the HR listener rule (D6).
- **Still failing:** redeploy the previous version (D9-L) and tell the administrator.

### D9-L. ECS: roll back a new image

- The service has a **deployment circuit breaker**, so a version that cannot start is rolled back automatically. If a version starts but behaves badly, go back by hand. Find the earlier task definition revision:
  ```
  aws ecs list-task-definitions --family-prefix hr-onboarding-portal --sort DESC
  aws ecs update-service --cluster hr-onboarding --service portal --task-definition hr-onboarding-portal:<OLD_REVISION>
  ```
  (Repeat for `hr-onboarding-hr`.) Wait about two minutes, test the site, and read the log (Step 8).
- To release a new version: push a new tag (`v2`), register a new task definition revision pointing to it, and run `update-service` with it. Test it first on a copy service (for example `portal-test` with its own target group) before changing the real one.

### D9-P. ECS: the schedule did not work

- `aws application-autoscaling describe-scheduled-actions --service-namespace ecs` lists the four actions with their times and time zone. Each must say `Asia/Kolkata`. `describe-scaling-activities` shows what ran and any error.
- If the scheduled actions are missing, an error mentions a missing service-linked role: ask the administrator to allow `CreateServiceLinkedRole` for Application Auto Scaling (Part 0.4).
- Use the manual switch in D7 to fix the state immediately, then fix the schedule.
- Check the live state: `aws ecs describe-services --cluster hr-onboarding --services portal hr --query "services[].{name:serviceName,desired:desiredCount,running:runningCount}"`.

### D9-S. ECS: MongoDB problems (NAT gateway address)

- Atlas is outside AWS, so check three things in order:
  - **Atlas Network Access** must list `<NAT_IP>` (VPC → NAT gateways → the address). If the NAT gateway was deleted and recreated, the address changed: add the new one. Most common cause.
  - **The task can reach port 27017:** `hr-onboarding-task-sg` outbound must allow 27017 (D1) and the private subnets must route to the NAT gateway.
  - **The string and password:** `/hr-onboarding/mongodb-uri` has the right user, password and cluster name; the user has readWrite on `hr_onboarding`; the cluster is not paused (free and some tiers pause when idle).
- Atlas → **Metrics** and **Real-time** show connections arriving. No connections at all points to the network (first two bullets); "authentication failed" points to the string.
- If Atlas itself is down, check https://status.mongodb.com. The portals show errors until it is back; nothing already stored is lost.

