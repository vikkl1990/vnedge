# VM deployment and conservative cleanup

From the local repository, after tests, review, commit and push to origin/main:

```bash
export VNEDGE_VM_SSH_TARGET=ubuntu@161.118.252.185
export VNEDGE_VM_SSH_KEY=/absolute/path/to/your/ssh-key
bash scripts/deploy_vm.sh --check
bash scripts/deploy_vm.sh --deploy
bash scripts/deploy_vm.sh --cleanup-build-cache
```

The destination is explicitly `/home/ubuntu/vnedge`. Check is read-only and is
the default. Deploy refuses dirty checkouts or a main revision different from
local HEAD. It preserves prior running images under rollback tags and writes
non-secret revision/service metadata to a fresh `vnedge-deploy-backup.*` directory.
It uses the existing serialized `scripts/deploy.sh`: build once, recorder-first
restoration, bounded service waves, TLS checks, capital-policy and readiness
checks. Research and analyst profiles are included. No live gates are opened.

Do not run concurrent manual compose recreation. A failed deploy preserves
running services and reports failure; do not interpret a running UI as ready.
Rollback is an operator-reviewed action using the previous revision and retained
image tags; this wrapper deliberately does not perform automatic rollback over
new durable data. Recorder restarts can create capture gaps; inspect coverage
after deployment, not just process health.

Cleanup takes the deployment lock and prunes only unused Docker build cache
older than seven days. Cache is regenerable. It does not delete market archives,
canonical candles, journals, database files, settings, secrets, Docker volumes,
containers, source trees or rollback images. A full VM reset or a change in
running-service scope requires separate explicit instructions.

Research Parquet inputs remain gitignored data, not production scanner feeds.
Research code/contracts/reports can be deployed without activating a strategy.
