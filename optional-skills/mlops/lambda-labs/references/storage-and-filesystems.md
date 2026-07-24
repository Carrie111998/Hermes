# Lambda Labs Storage and Filesystems

What persists and what does not: creating and attaching persistent filesystems versus the ephemeral instance root volume.

## Filesystems

Filesystems persist data across instance restarts and terminations:

```bash
# Mount location
/lambda/nfs/<FILESYSTEM_NAME>

# Example: save checkpoints
python train.py --checkpoint-dir /lambda/nfs/my-storage/checkpoints
```

## Create filesystem

1. Go to Storage in Lambda console
2. Click "Create filesystem"
3. Select region (must match instance region)
4. Name and create

## Attach to instance

Filesystems must be attached at instance launch time:
- Via console: Select filesystem when launching
- Via API: Include `file_system_names` in launch request

A running instance cannot have a filesystem attached — terminate and relaunch.

## Layout best practices

`/home/ubuntu` is the default user's home on the instance root volume. It is fast
local NVMe but **ephemeral**: everything there is destroyed on termination.

```bash
# Store on filesystem (persists)
/lambda/nfs/storage/
  ├── datasets/
  ├── checkpoints/
  ├── models/
  └── outputs/

# Local SSD (faster, ephemeral)
/home/ubuntu/
  └── working/  # Temporary files
```

Rule of thumb: intermediate/scratch files on local SSD for speed, checkpoints and
final outputs on the filesystem for durability.

1-Click Cluster compute nodes additionally have 24 TB NVMe each — also ephemeral.

See `advanced-usage.md` (Advanced Filesystem Usage) for S3/rclone data staging and
sharing checkpoints between instances.
