# Lambda Labs Access and Environment

How to reach an instance (SSH, keys, tunnels, JupyterLab), what the pre-installed Lambda Stack contains, and the networking/firewall rules that constrain access.

`ubuntu` is the default login user on Lambda GPU instances, and its home
(`/home/ubuntu`) sits on the instance's **ephemeral** root volume.

## Connect via SSH

```bash
# Get instance IP from console
ssh ubuntu@<INSTANCE-IP>

# Or with specific key
ssh -i ~/.ssh/lambda_key ubuntu@<INSTANCE-IP>
```

## SSH keys

```bash
# Generate key locally
ssh-keygen -t ed25519 -f ~/.ssh/lambda_key

# Add public key to Lambda console
# Or via API (see api-and-cli.md)
```

Keys must exist in the Lambda console **before** the instance launches; a key
added afterwards will not be injected.

### Multiple keys

```bash
# On instance, add more keys
echo 'ssh-rsa AAAA...' >> ~/.ssh/authorized_keys
```

### Import from GitHub

```bash
# On instance
ssh-import-id gh:username
```

## SSH tunneling

Only port 22 is open by default, so tunnel everything else.

```bash
# Forward Jupyter
ssh -L 8888:localhost:8888 ubuntu@<IP>

# Forward TensorBoard
ssh -L 6006:localhost:6006 ubuntu@<IP>

# Multiple ports
ssh -L 8888:localhost:8888 -L 6006:localhost:6006 ubuntu@<IP>
```

## JupyterLab

### Launch from console

1. Go to Instances page
2. Click "Launch" in Cloud IDE column
3. JupyterLab opens in browser

### Manual access

```bash
# On instance
jupyter lab --ip=0.0.0.0 --port=8888

# From local machine with tunnel
ssh -L 8888:localhost:8888 ubuntu@<IP>
# Open http://localhost:8888
```

## Lambda Stack

All instances come with Lambda Stack pre-installed:

```bash
# Included software
- Ubuntu 22.04 LTS
- NVIDIA drivers (latest)
- CUDA 12.x
- cuDNN 8.x
- NCCL (for multi-GPU)
- PyTorch (latest)
- TensorFlow (latest)
- JAX
- JupyterLab
```

### Verify installation

```bash
# Check GPU
nvidia-smi

# Check PyTorch
python -c "import torch; print(torch.cuda.is_available())"

# Check CUDA version
nvcc --version
```

Do not modify the system Python — create a venv or conda env instead
(see `advanced-usage.md`, Environment Management).

## Networking

### Bandwidth

- Inter-instance (same region): up to 200 Gbps
- Internet outbound: 20 Gbps max

### Firewall

- Default: Only port 22 (SSH) open
- Configure additional ports in Lambda console
- ICMP traffic allowed by default

### Private IPs

```bash
# Find private IP
ip addr show | grep 'inet '
```

Multi-node jobs must run in the same region and need the distributed port open
(29500 for PyTorch by default).
