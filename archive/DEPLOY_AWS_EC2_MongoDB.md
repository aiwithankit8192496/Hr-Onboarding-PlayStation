# HR Onboarding: low-cost deployment on AWS

> This is the short runbook. The full step-by-step guide, with prices, troubleshooting, the security checklist and the
> ECS scale-up, is [AWS_SETUP_GUIDE.md](AWS_SETUP_GUIDE.md) (Steps 12 to 19 there match sections 5 to 9 here).

Sized for **a handful of applications a day (7 to 9)**. Both portals run as containers on **one small EC2 instance**,
with Caddy in front for HTTPS. There is no load balancer, no NAT gateway, no autoscaling and no ECS: those cost far
more than this load needs. Each student's record is a document in **MongoDB**; each student's uploaded documents
live in a **private S3 bucket**; the HR tables are written to the **shared bucket `igtl-hr-analytics-ayush-2026`**,
where the HR portal and Athena read them.

```
   employees ──HTTPS──▶ onboard.example.com ─┐
                                              ├──▶  Elastic IP ──▶  EC2 t4g.small (one instance, public subnet)
   HR team  ───HTTPS──▶ hr.example.com ──────┘                       ├─ caddy        :80/:443   HTTPS, certificates
                                                                     ├─ onboarding   :8000      (internal only)
                                                                     └─ hr           :8001      (internal only)
                                                                          │                │
                                    MongoDB Atlas (allows only the Elastic IP)         S3 (instance role, no keys)
                                    applications / forms / counters                      igtl-hr-analytics-ayush-2026   (shared, colleague's)
                                                                                           employees/<id>.parquet, employee_education/<id>.parquet
                                                                                         yourcompany-hr-private          (ID documents + sensitive tables)
                                                                                           onboarding/employees/<id>/documents/*
                                                                                           hr/employee_bank_details/, hr/employee_sensitive_details/
```

The containers keep nothing on their own disk (records are in MongoDB, files in S3), so the instance is disposable:
if it dies you start a new one and run the same commands (section 11).

**Cheap now, scalable later, by design.** The image, the settings and the data layout are exactly what an ECS Fargate
deployment would use. Today one instance runs them; when a client needs more users or high availability you move the
**same image and the same `.env` values** to ECS behind a load balancer (section 14) without touching the app or the
data. You pay for the big design only when you need it.

## 0a. You have already done Steps 1 to 12 of `AWS_SETUP_GUIDE.md`: what carries over

| What you did | Use it here? |
|---|---|
| Step 2 budget, Step 3 SNS topic `hr-onboarding-alerts` | **Yes.** Set the budget to about **$40** (the guide suggests $120 for the big design). The alarms in 12.1 send to the same topic. |
| Step 4 logs bucket, Step 5 CloudTrail, Step 6 log groups, Step 7 alarms | **Yes, unchanged.** The containers write to the log groups `/ecs/hr-onboarding-portal` and `/ecs/hr-onboarding-hr` (compose file). The CloudTrail alarms and the `ERROR` metric filter stay. Skip only the ALB and ECS alarms in Step 7. |
| Step 9 KMS key | **Yes** for secrets and logs, as in the guide. Leave `S3_KMS_KEY_ID` empty (Step 10c). |
| Step 10 data buckets (and 10i, if you added it) | **Yes, unchanged.** Use the role name `hr-onboarding-task-role` (5.2) so the bucket policies you already wrote, and the one you may have sent your colleague, stay correct. |
| Step 12 VPC, subnets, security groups | **Partly.** Launch the instance in one of the **public** subnets of your VPC. **The NAT gateway is not needed**: see the box below. |

> **Do this now if you created the NAT gateway in Step 12:** it costs about **$40 a month** plus its Elastic IP, and the
> instance in a public subnet does not need it. VPC console → *NAT gateways* → select it → *Delete*, then *Elastic IPs* →
> *Release* the address it used (a released-but-unreleased address is still billed). Keep the VPC, the subnets, the free S3
> gateway endpoint and the security groups: they cost nothing and the scale-up in section 14 reuses them (you would
> recreate a NAT gateway then, or use another way to give MongoDB a fixed address). If you have not run Step 12 yet, create
> the VPC with **NAT gateways: None**.

## 0. What it costs (approximate: check the AWS Pricing Calculator for your region)

| Item | Per month | Note |
|---|---|---|
| EC2 `t4g.small` (2 vCPU, 2 GB, ARM), always on | about $12 to $15 | `t4g.micro` (1 GB) is too tight for PDF reading |
| EBS disk, 20 GB gp3 | about $2 | |
| Public IPv4 address (the Elastic IP) | about $3.65 | $0.005 per hour |
| CloudWatch logs and 2 alarms (Steps 6, 7, 12.1) | under $1 | |
| KMS key (Step 9) | $1 | already created |
| S3 (documents, Parquet files, backups) | under $1 | a few hundred students is well under 1 GB |
| Route 53 hosted zone (skip it if your DNS is elsewhere) | $0.50 | |
| HTTPS certificates (Caddy / Let's Encrypt) | $0 | |
| Secrets and settings (`.env` file on the instance) | $0 | |
| MongoDB Atlas | $0 to about $10+ | billed by MongoDB, not AWS: see section 4 |
| Gemini API | cents | 9 applications a day is tiny; check Google's current price |
| **Total** | **about $22 a month plus Atlas** | with the NAT gateway removed (section 0a) |

The earlier design (ECS Fargate, load balancer, NAT gateway) was roughly four times this. What you give up: no automatic
failover (one instance, one zone) and no scaling. For 7 to 9 applications a day neither is needed; an outage costs you
about 15 minutes to rebuild (section 11), and nothing is lost because the data is not on the instance.

Set an **AWS budget of $40 a month** (Billing → Budgets) so any surprise is emailed to you.

## 1. What is stored where

**One folder per student.** The ID (`IGTPL007`) is issued by an atomic counter in MongoDB, so two requests can
never give the same ID to two students. That ID is the student's MongoDB `_id` and their S3 folder name:

| Data | Where | Key / id |
|---|---|---|
| Whole application (personal, family, education, bank, references, policies, flags, status) | MongoDB `applications` | `_id = IGTPL007` |
| The form as submitted (so a flagged form can be reopened) | MongoDB `forms` | `_id = IGTPL007` |
| Uploaded documents: Aadhaar front/back, PAN, photo, education certificate, resume, bank proof, payslips, relieving letter | S3 (private bucket) | `onboarding/employees/IGTPL007/documents/<file>` |
| HR tables, one small Parquet file per student | S3 | `employees/IGTPL007.parquet`, `employee_education/...` (shared bucket, top level); `hr/employee_bank_details/...`, `hr/employee_sensitive_details/...` (your private bucket) |

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
   their side (section 3.2) **and** the IAM policy on your side (section 5.2). Same account: only the IAM policy.
2. **Which region is it in?** `aws s3api get-bucket-location --bucket igtl-hr-analytics-ayush-2026`
3. **How is it encrypted?** SSE-S3 (default) works as is. If it uses their own KMS key, they must also allow your role
   in the key policy, and you set `S3_KMS_KEY_ID`.
4. **Is "Bucket owner enforced" on?** (S3 default.) Then files you write are owned by them, and their Athena can read them.

What the app does in that bucket: it **only writes its own files**: `employees/IGTPL###.parquet` and `employee_education/IGTPL###.parquet`
(top level, no `hr/` prefix, matching the live Athena tables; `SHARED_TABLE_PREFIX` changes this). It never lists,
overwrites or deletes your colleague's files.

### 2.2 Keep PAN, Aadhaar and bank numbers out of the shared bucket (required)
Two things would otherwise be visible to everyone who can read your colleague's bucket or query their Athena tables:

- the uploaded ID documents (Aadhaar and PAN images), and
- the `employee_bank_details` and `employee_sensitive_details` tables. In those, the columns named `*_placeholder`
  hold the **real** PAN, Aadhaar, phone and account numbers.

So create **one private bucket of your own** (section 5) and set both `S3_DOCUMENTS_BUCKET` and
`S3_RESTRICTED_BUCKET` to it. The shared bucket then receives only `employees` and `employee_education`. The app
refuses to start if either is missing or equal to the shared bucket.

Also: your Athena users will see real students next to the 100 synthetic ones in `employees`
(filter with `employee_id LIKE 'IGTPL%'`).

### 2.3 Use `mongo_s3`, not `s3`
`STORAGE_BACKEND=s3` would save each student's whole record (including PAN, Aadhaar and bank numbers) as JSON in
`S3_BUCKET`, which is the shared bucket. Keep `mongo_s3`: the records then stay in MongoDB.

### 2.4 Domain
You need a domain you control (e.g. `example.com`) for HTTPS: one name for employees (`onboard.example.com`) and one
for HR (`hr.example.com`). The HR portal carries a password and personal data, so it must not run over plain HTTP.

## 3. Before you start

You need: an AWS account, the **AWS CLI** installed on your computer, and `aws configure` done. Choose a region (use
the bucket's region) and replace `REGION` and `ACCOUNT_ID` in every command below. You do **not** need Docker on your
own computer: the image is built on the instance.

### 3.1 Check you can reach the shared bucket
```
aws s3 ls s3://igtl-hr-analytics-ayush-2026/employees/ --region REGION
```
A list (or an empty result) means yes. `AccessDenied` means ask your colleague for 3.2.

### 3.2 What your colleague adds (only if the bucket is in another account)
Send them this; they add it to the bucket policy of `igtl-hr-analytics-ayush-2026`. Fill in `YOUR_ACCOUNT_ID` after you
create the role in section 5.2:
```json
{
  "Sid": "HROnboardingApp",
  "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::YOUR_ACCOUNT_ID:role/hr-onboarding-task-role" },
  "Action": ["s3:GetObject", "s3:PutObject"],
  "Resource": [
    "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
    "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*"
  ]
}
```
plus one more statement allowing `s3:ListBucket` on `arn:aws:s3:::igtl-hr-analytics-ayush-2026` for the same principal.

## 4. MongoDB (Atlas)
1. Create an account at mongodb.com/atlas and a cluster in the same region as your AWS setup.
2. **Which tier:** the free **M0** costs nothing, but has **no backups**. Either accept that and use the nightly dump in
   section 10, or pick a small paid tier that includes backups. Read the current tiers and prices at
   https://www.mongodb.com/pricing (I have not re-checked them). At 9 applications a day, size is not the question; backups are.
3. *Database Access* → add a user (e.g. `hr_app`) with a strong password and the role **readWrite on `hr_onboarding`**.
4. *Network Access* → for now add your own IP, to test. In section 5 you will replace it with the instance's Elastic IP.
   Never leave `0.0.0.0/0` in there.
5. *Connect* → *Drivers* → copy the connection string:
   `mongodb+srv://hr_app:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`. This is `MONGODB_URI`.
   The collections (`applications`, `forms`, `counters`) are created automatically on the first submission.

## 5. AWS: bucket, role, instance

### 5.1 Private bucket (documents and sensitive tables)
S3 console → *Create bucket*: name e.g. `yourcompany-hr-private`, same region, **Block all public access = on**,
**Bucket Versioning = on**, default encryption **SSE-S3** (or KMS). Add a lifecycle rule that deletes
non-current versions and anything under `backups/` after 30 days, so versioning does not grow the bill.

### 5.2 Role for the instance
IAM → *Create role* → trusted entity **EC2** → name `hr-onboarding-task-role` (the name the guide uses, so existing bucket
policies keep working; the console also creates an instance profile of the same name). Attach the AWS-managed policy
`AmazonSSMManagedInstanceCore` (so you can open a shell without SSH) and add this inline policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SharedBucketOwnFiles", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": ["arn:aws:s3:::igtl-hr-analytics-ayush-2026/employees/*",
                   "arn:aws:s3:::igtl-hr-analytics-ayush-2026/employee_education/*"] },
    { "Sid": "SharedBucketList", "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::igtl-hr-analytics-ayush-2026",
      "Condition": { "StringLike": { "s3:prefix": ["employees/*", "employee_education/*"] } } },
    { "Sid": "PrivateBucket", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::yourcompany-hr-private/*" },
    { "Sid": "PrivateBucketList", "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::yourcompany-hr-private" },
    { "Sid": "ContainerLogs", "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": ["arn:aws:logs:REGION:ACCOUNT_ID:log-group:/ecs/hr-onboarding-portal:*",
                   "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/ecs/hr-onboarding-hr:*"] }
  ]
}
```
In the policy above, the two data buckets are shown as one `yourcompany-hr-private`. You have separate documents and
restricted buckets (Step 10): repeat the `PrivateBucket` and `PrivateBucketList` statements for each (list both ARNs in
`Resource`). (If you use a KMS key on S3, also allow `kms:GenerateDataKey` and `kms:Decrypt` on it.) The role has no
delete permission on the shared bucket, on purpose. There are no access keys anywhere: the app uses this role.

### 5.3 Security group
EC2 → *Security groups* → create `hr-onboarding-sg` in the VPC from Step 12 (the existing `hr-onboarding-alb-sg` has the
same inbound rules and would also work): inbound **TCP 80 and 443 from anywhere**
(80 only redirects to HTTPS and lets Caddy get certificates). **No inbound SSH**; use Session Manager (5.4). Outbound: default (all).
To let only your office reach the HR name, see section 9.

### 5.4 Launch the instance
EC2 → *Launch instance*:

- Name `hr-onboarding`, AMI **Amazon Linux 2023** (64-bit **Arm**), type **t4g.small**.
- No key pair (you will use Session Manager).
- Network: your VPC from Step 12, one of its **public** subnets (`<PUBLIC_A>`), **auto-assign public IP: off** (you attach an Elastic IP next).
- Security group `hr-onboarding-sg`; storage **20 GiB gp3**, **encrypted**.
- *Advanced details* → IAM instance profile `hr-onboarding-task-role`; **Metadata version: V2 only** (IMDSv2 required).

Then EC2 → *Elastic IPs* → *Allocate*, and *Associate* it with the instance. Note it as `ELASTIC_IP`.

- Atlas *Network Access*: add `ELASTIC_IP/32` and delete your own IP.
- In your DNS, create **A records** `onboard.example.com` and `hr.example.com` → `ELASTIC_IP`.

## 6. Put the app on the instance

EC2 console → select the instance → *Connect* → **Session Manager**. Then:

```
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64 -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
sudo mkdir -p /opt/hr && sudo chown ssm-user /opt/hr
git clone https://github.com/aiwithankit8192496/Hr-Onboarding-PlayStation.git /opt/hr
```
(If the repository is private, clone with a read-only token or deploy key. Never put the token in a file in the repo.
A warning about `buildx` from compose can be ignored.)

## 7. Settings

Create `/opt/hr/.env` (`nano /opt/hr/.env`), then `chmod 600 /opt/hr/.env`. This file is not in git and not in the image;
it is the only place the secrets live on the instance.

```
ONBOARD_DOMAIN=onboard.example.com
HR_DOMAIN=hr.example.com
ONBOARDING_URL=https://onboard.example.com

STORAGE_BACKEND=mongo_s3
AWS_REGION=REGION
S3_BUCKET=igtl-hr-analytics-ayush-2026
S3_PREFIX=onboarding/
S3_DOCUMENTS_BUCKET=yourcompany-hr-private
S3_RESTRICTED_BUCKET=yourcompany-hr-private
MONGODB_URI=mongodb+srv://hr_app:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=hr_onboarding

GEMINI_API_KEY=...
GEMMA_MODEL=gemma-4-26b-a4b-it

HR_USER=hr
HR_PASSWORD=at-least-12-characters-use-20-random-ones
```
The full list of settings is in the appendix. The app checks them on start and refuses to run (naming the variable)
if the buckets are missing or not separate.

## 8. Start it
```
cd /opt/hr
docker compose -f docker-compose.aws.yml up -d --build
docker compose -f docker-compose.aws.yml ps
docker compose -f docker-compose.aws.yml logs --tail 50
```
The first build takes a few minutes. Open `https://onboard.example.com` and `https://hr.example.com`; Caddy gets the
certificates on the first request (DNS must already point at the Elastic IP). The containers restart by themselves
after a crash or a reboot, because Docker starts at boot and the services use `restart: unless-stopped`.

`docker-compose.aws.yml` and `Caddyfile` in the project folder define this: the two portals are only reachable through
Caddy, never directly.

## 9. Optional: only your office can reach the HR portal
The HR portal already has a login. To also limit it by address, change the HR block in `Caddyfile` to
```
{$HR_DOMAIN} {
	@office remote_ip 203.0.113.0/24
	handle @office {
		reverse_proxy hr:8001
	}
	respond 403
}
```
(your office or VPN range instead of `203.0.113.0/24`), then `docker compose -f docker-compose.aws.yml restart caddy`.

## 9b. Optional: switch the instance off from 12 am to 8 am
If the portals must be off at night (the company rule in `AWS_SETUP_GUIDE.md`), stop the instance on a schedule. It saves
about a third of the EC2 cost; the Elastic IP, disk and data stay. In the console: *Amazon EventBridge* → *Scheduler* →
*Create schedule*, twice:

| Name | Schedule | Target |
|---|---|---|
| `hr-stop` | recurring, cron `0 0 * * ? *`, time zone **Asia/Kolkata** | *All APIs* → **Amazon EC2 → StopInstances**, input `{"InstanceIds":["i-xxxxxxxx"]}` |
| `hr-start` | recurring, cron `0 8 * * ? *`, time zone **Asia/Kolkata** | **Amazon EC2 → StartInstances**, same input |

Let the console create the execution role for each. EventBridge Scheduler's first 14 million invocations a month are free
(I have not re-checked this). After a start, Docker and the containers come back by themselves in a few minutes.
MongoDB Atlas and S3 stay on all night, so the Atlas bill does not stop.

## 10. Move your data in, and back it up

**Existing applications.** Your current 9 applications live in the `submissions/`, `forms/` and `uploads/` folders. From
the project folder on your computer, with your own IP in Atlas *Network Access* and AWS credentials in place (a
temporary access key is fine), set these in the terminal and run:
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
all the tables from MongoDB. Delete the temporary key and your Atlas IP afterwards.

**Nightly backup of MongoDB** (needed on the free Atlas tier; harmless on others). On the instance:
```
sudo tee /etc/cron.d/hr-backup >/dev/null <<'EOF'
30 23 * * * root . /opt/hr/.env; docker run --rm mongo:7 mongodump --uri="$MONGODB_URI" --db hr_onboarding --archive --gzip | aws s3 cp - s3://yourcompany-hr-private/backups/mongo-$(date +\%F).archive.gz
EOF
```
It runs at 11:30 pm server time (UTC unless you change it), so with the night shutdown of 9b check that the instance is running
at that time. The lifecycle rule from 5.1 deletes old backups after 30 days. Restoring is `mongorestore --archive --gzip` with the
same image. Test one restore into a scratch database before you rely on it. (The cron line needs `MONGODB_URI` without
characters the shell would interpret; quote it in `.env` if the password has any.)

## 11. Check that it works
1. Open `https://onboard.example.com`, fill a test application and submit.
2. MongoDB Atlas → *Browse Collections*: a new document in `applications` with your ID.
3. S3: `yourcompany-hr-private/onboarding/employees/<ID>/documents/` holds the uploads, and
   `igtl-hr-analytics-ayush-2026/employees/<ID>.parquet` exists.
4. Open `https://hr.example.com`, log in, and the application is listed with its documents.
5. Approve it in the HR portal: the status in `employees/<ID>.parquet` becomes `Approved - Joining`.
6. Athena: `SELECT employee_id, full_name, employment_status FROM hr_analytics.employees WHERE employee_id LIKE 'IGTPL%'`.
   (If your colleague's tables exist already, this works as is. Otherwise run `Data/sources_hr/athena_tables.sql` with
   the bucket name filled in.)
7. Reboot the instance (EC2 → *Instance state* → *Reboot*): after a few minutes both sites are back by themselves.

**If the instance is lost:** launch a new one as in 5.4, move the Elastic IP to it (*Associate*), and repeat sections 6
to 8. No data is lost: it is in MongoDB and S3. Caddy asks for new certificates automatically.

## 12. Running it
- **Logs:** CloudWatch → Logs → `/ecs/hr-onboarding-portal` and `/ecs/hr-onboarding-hr` (retention and the `ERROR`
  alarm from Steps 6 and 7 apply). `docker compose -f docker-compose.aws.yml logs --tail 100 onboarding` (or `hr`, `caddy`) shows recent lines on
  the server too. A failed table write is logged as
  `HR table sync failed` and the student's record is still saved; run `python sync_tables.py` to repair.
- **Releasing a new version:** `cd /opt/hr && git pull && docker compose -f docker-compose.aws.yml up -d --build`. The
  portals are down for a few seconds. Do it outside working hours. To roll back, `git checkout <previous commit>` and run the same command.
- **Updates to the machine:** once a month, `sudo dnf upgrade -y` and a reboot.
- **Memory:** `docker stats --no-stream`. If the onboarding container is near its 900 MB limit or keeps restarting
  (exit code 137), move to `t4g.medium` (stop the instance, change the type, start it; the Elastic IP stays).
- **Backups:** Atlas backups (if your tier has them) plus the nightly dump; S3 versioning covers documents and tables.
- **Access:** give people their own IAM users or SSO, never share the root account, and turn on MFA.

### 12.1 Alarms for the instance
CloudWatch → *Alarms* → *Create alarm* → *EC2 → Per-Instance Metrics*, action **send to the SNS topic
`hr-onboarding-alerts`** (Step 3), **treat missing data as "not breaching"** (the instance is off at night if you use 9b):

| Alarm | Metric | Condition |
|---|---|---|
| `hr-instance-status` | `StatusCheckFailed` | ≥ 1 for 5 minutes (the machine is unhealthy: reboot it) |
| `hr-instance-cpu` | `CPUUtilization` | ≥ 80 % for 15 minutes (time to look at section 14) |

Together with the CloudTrail and `ERROR` alarms from Step 7 that is enough for this size. Memory is not shown by default
(it needs the CloudWatch agent, which is extra); `docker stats` and exit code 137 tell you. $0.10 per alarm per month.

## 13. Troubleshooting
| Symptom | Likely cause |
|---|---|
| A container stops straight away, log says `Unsafe storage configuration` | a bucket setting in `.env` is missing or the buckets are not separate (section 7) |
| Log says `STORAGE_BACKEND=mongo_s3 needs MONGODB_URI` | `MONGODB_URI` is missing in `.env` |
| Log shows `ServerSelectionTimeoutError` | the Elastic IP is not in Atlas *Network Access*, or the URI/password is wrong |
| Submit fails with `AccessDenied` on S3 | the instance role policy (5.2), or your colleague's bucket policy (3.2), or KMS key access |
| Browser says the certificate is invalid, or the site does not open | the DNS records do not point at the Elastic IP yet, or ports 80/443 are not open in `hr-onboarding-sg`; see `logs caddy` |
| `502 Bad Gateway` from Caddy | the portal container is restarting: see `docker compose ... ps` and its logs |
| HR portal asks for a login you don't know | `HR_USER` and `HR_PASSWORD` in `.env`; after changing them run `docker compose -f docker-compose.aws.yml up -d` |
| The `hr` container stops at start, log says `HR_USER and HR_PASSWORD are not both set` or `HR_PASSWORD is shorter than 12` | set both in `.env` (password 12+ characters; use 20+ random), then `up -d` again |
| Table files exist in S3 but not in Athena | the Glue tables are missing or point at another location; run `athena_tables.sql` |
| Cannot open a shell | the role lacks `AmazonSSMManagedInstanceCore`, or the instance has no route to the internet |

## 13b. Data security: what is protected, and what you must still decide

**Built in**
- **HR portal login is mandatory.** With any backend except `local` the HR portal refuses to start without `HR_USER` and
  `HR_PASSWORD` (password at least 12 characters; use 20+ random). Test it: open the HR address in a private window, it must ask for a password.
- **Edit-link tokens stay out of logs.** The portals' web access log is off, because the employee's edit link has a secret
  token in its URL. Do not turn it back on, and do not add any logging in front of the app (Caddy `log`, a load balancer's
  access logs) that records query strings.
- **Browser headers** from the `Caddyfile`: HTTPS only for a year (HSTS), no content sniffing, no framing, no referrer, and
  `no-store`, so ID documents and personal data are not kept in the browser cache. There is no Content-Security-Policy
  because the pages compile their code in the browser.
- **Separate buckets** for documents and the bank and sensitive tables, enforced at start-up; HTTPS-only and encrypted S3.

**Decide before a real launch** (the app cannot do these for you)
| Risk | What to do |
|---|---|
| PAN, Aadhaar and account numbers are stored unmasked in MongoDB records and the restricted Parquet files | Rely on encryption at rest (S3 default encryption; confirm MongoDB Atlas encryption at rest for your tier) and tight access. For Aadhaar, take advice on whether the full number may be stored at all. |
| The Aadhaar, PAN and payslip images are sent to Google's Gemini API | Use a **paid** plan whose terms say your data is not used to improve Google's products (read the terms for your tier; free tiers may differ) and set a spending cap on the key. Say so in the client contract. |
| One shared HR password: no record of who opened whose documents | Fine for a small team that trusts each other; for a client, plan per-person logins. The S3 data events from Step 5 show *that* a file was read, not *which person*. |
| The HR login has no lockout on repeated guesses | Use a long random password. Optionally limit the HR name to your office range (section 9). |
| The onboarding page is public and its AI-extraction calls are open to anyone | Set the Gemini spending cap. Stock Caddy has no rate limiting; for more, put AWS WAF or a Cloudflare-style proxy in front, or build Caddy with a rate-limit plugin. |
| The pages load React and Babel from unpkg without an integrity check | A compromised CDN could run code on the page that shows Aadhaar. Self-host those scripts (or pin them with integrity hashes) before a client launch. |
| Edit-link tokens never expire | The link only reopens the form while HR has flagged it; still, treat the emailed link as private. |
| The nightly MongoDB dump holds every record | Keep `backups/` readable only by the instance role and administrators (bucket policy 10i style), and keep the 30-day lifecycle rule. |

## 14. Growing later: the upgrade path

The app is stateless (records in MongoDB, files in S3, IDs from an atomic MongoDB counter), so any number of copies can
run side by side. Move up one step at a time, only when a measurement says so (CPU alarm, exit code 137, slow pages):

| Stage | When | What you do | Extra cost |
|---|---|---|---|
| **1. Bigger machine** | memory near the limit, CPU alarm | Stop the instance, change the type (`t4g.medium`, then `t4g.large`), start it. The Elastic IP and everything else stay. About 5 minutes. | the larger instance's price |
| **2. Bigger database** | Atlas storage or connection limits, or you want backups and failover | Change the Atlas tier. Nothing changes in the app. | Atlas bill |
| **3. Two or more copies, high availability** | a client needs no downtime, or many people submit at once | Move to **ECS Fargate behind an Application Load Balancer**: the same image and the same `.env` values as task settings. Follow `AWS_SETUP_GUIDE.md` Steps 13 to 19 and 21 (registry, task definitions, services, load balancer, autoscaling). The VPC, subnets, security groups, log groups, alarms, buckets and role (add the ECS tasks service to its trust policy) from Steps 1 to 12 are already there. MongoDB needs a fixed address again: recreate the NAT gateway as that guide describes. | about the figures in `AWS_SETUP_GUIDE.md` Part 2.5 |
| **4. Many clients** | you are selling it | One stack per client, see section 15. | one stack each |

Things to know before promising a client numbers:
- **PDF reading is one at a time inside each container** (a lock in `react_onboard.py` keeps the PDF library from crashing
  the process). A single container is therefore fine for occasional submissions but handles several simultaneous
  PDF uploads one after another; more containers (stage 3) is the fix, not a bigger machine.
- **Gemini** is the other limit: document extraction calls it, so check your API quota and price for the volume you expect.
- **I have not load-tested this.** Before a client is told "supports N users", run a test with that many simultaneous
  submissions against a copy of the setup and watch CPU, memory and response times.
- A `t4g` instance is "burstable": sustained high CPU uses up its credits and can cost extra. That is a sign to go to
  stage 1 or 3, not to ignore.

## 15. Selling it to clients

The cleanest model for this app, which holds PAN, Aadhaar and bank details, is **one separate deployment per client**,
ideally in the **client's own AWS account** (their data, their bill, no mixing). Everything that differs per client is a
setting; the code does not change:

| Per client | Setting |
|---|---|
| Their buckets | `S3_BUCKET` (their analytics bucket; it must differ from the next two), `S3_DOCUMENTS_BUCKET`, `S3_RESTRICTED_BUCKET` |
| Their database | `MONGODB_URI`, `MONGODB_DB` |
| Their names | `ONBOARD_DOMAIN`, `HR_DOMAIN`, `ONBOARDING_URL` |
| Their HR login | `HR_USER`, `HR_PASSWORD` |
| Their extraction key | `GEMINI_API_KEY` (so their usage is on their quota and bill) |
| Their table layout | `SHARED_TABLE_PREFIX` (empty means top level) |

Notes:
- The shared bucket `igtl-hr-analytics-ayush-2026` is **your colleague's and specific to this deployment**. A client has no
  such bucket: they use their own analytics bucket as `S3_BUCKET`, with the Athena tables from `Data/sources_hr/athena_tables.sql`.
  The app still needs three separate buckets and refuses to start otherwise.
- Sections 5 to 8 of this guide are the runbook for one client. Write down the values you used in a per-client sheet; about
  an hour of work per client once you have done it once.
- To make that repeatable (and to avoid console mistakes) turn sections 5 to 8 into Terraform or CloudFormation later. The
  `main.tf` in this folder describes an older design and is **not** that; do not apply it.
- The root `README.md` still describes the old Streamlit version. Update it before anyone outside sees the repository.
- This app processes identity and bank data of people in India. Before you sell it, take advice on the data-protection and
  Aadhaar rules that apply (who is responsible for the data, retention, breach notice), and put the answers in your client contract.
  I cannot give you that advice.

## Appendix: all settings
| Variable | Meaning |
|---|---|
| `ONBOARD_DOMAIN`, `HR_DOMAIN` | the two public names (used by Caddy) |
| `STORAGE_BACKEND` | `mongo_s3` (this deployment), `s3` (records as JSON in `S3_BUCKET`: not for the shared bucket, see 2.3), `local` (default, files on disk) |
| `MONGODB_URI`, `MONGODB_DB` | MongoDB connection string (secret) and database name (default `hr_onboarding`) |
| `S3_BUCKET` | the shared bucket: only the `employees` and `employee_education` tables |
| `S3_DOCUMENTS_BUCKET` | **required**: your private bucket for uploaded documents; must differ from `S3_BUCKET` |
| `S3_RESTRICTED_BUCKET` | **required**: your private bucket for the bank and sensitive tables; must differ from `S3_BUCKET` and `S3_DOCUMENTS_BUCKET` |
| `S3_PREFIX` | prefix for the per-student folders, e.g. `onboarding/`; **required** with `STORAGE_BACKEND=s3` |
| `SHARED_TABLE_PREFIX` | folder before `employees/` and `employee_education/`; default empty (top level) |
| `S3_KMS_KEY_ID` | encrypt objects with this KMS key (SSE-S3 otherwise) |
| `AWS_REGION` | region of the buckets |
| `GEMINI_API_KEY`, `GEMMA_MODEL` | document extraction (onboarding portal only) |
| `HR_USER`, `HR_PASSWORD` | HR portal login. **Required**: the HR portal refuses to start without both, or with a password under 12 characters, unless `STORAGE_BACKEND=local` |
| `ONBOARDING_URL` | public address of the onboarding portal, used in the HR portal's flagged-application link |
| `HOST`, `PORT` | listen address (`0.0.0.0` in the image) and port (8000 / 8001, set by the compose file) |

The app refuses to start (with an error naming the variable) if any of the bucket rules above is broken.
