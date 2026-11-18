# Archive: earlier designs (do not use for a new deployment)

These files describe designs that were replaced by the serverless one (AWS Lambda + DynamoDB + S3 + CloudFront), now described in `../AWS_SETUP_GUIDE.md`.

| File | What it is |
|---|---|
| `AWS_SETUP_GUIDE_EC2_MongoDB.md` | The previous setup guide: one small EC2 server with Docker Compose and MongoDB Atlas. Also holds the old ECS Fargate + load balancer + NAT gateway steps (Appendix D there) |
| `DEPLOY_AWS_EC2_MongoDB.md` | The short runbook for that EC2 + MongoDB variant |
| `AWS_WORKFLOW.html`, `.pdf`, `.png` | The architecture diagram of the older ECS Fargate design |
| `AWS_SETUP_GUIDE.pdf` | A PDF print of a much older version of the setup guide |

Kept for reference only. They mention MongoDB, a NAT gateway, an HTTP-Basic HR login and other things that no longer apply.
