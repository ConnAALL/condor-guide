# AALL Compute Cluster

The AALL has a pool of ~50 machines across three rooms in New London Hall.
HTCondor manages the pool: you submit jobs, Condor finds idle machines,
runs your code, and brings the results back. You don't need to know which
machine your code runs on.

**Pool size:** ~540 CPUs, 2 GPUs (RTX 5090), ~890 GB RAM total.

## Quick Start

### 1. Connect

SSH into the central manager:

```bash
ssh aall@136.244.224.136
```

Ask Jim for the SSH key if you don't have it.

### 2. Write a submit file

```
# hello.sub
universe   = vanilla
executable = /bin/hostname
output     = hello.$(ClusterId).$(ProcId).out
error      = hello.$(ClusterId).$(ProcId).err
log        = hello.log

should_transfer_files   = YES
when_to_transfer_output = ON_EXIT

queue 5
```

### 3. Submit and check

```bash
condor_submit hello.sub    # submit
condor_q                   # check status
cat hello.*.out            # read results
```

That's it. Five jobs run on five different machines, each writes the
hostname it ran on. Read on for real workloads.

---

## Submitting Jobs

### Python script with parameters

```
# experiment.sub
universe   = vanilla
executable = /usr/bin/python3
arguments  = simulate.py --seed $(ProcId) --output result_$(ProcId).json

transfer_input_files    = simulate.py, config.json
transfer_output_files   = result_$(ProcId).json
should_transfer_files   = YES
when_to_transfer_output = ON_EXIT

output = logs/$(ClusterId).$(ProcId).out
error  = logs/$(ClusterId).$(ProcId).err
log    = experiment.log

request_cpus   = 1
request_memory = 2G
request_disk   = 500M

queue 100
```

Condor transfers `simulate.py` and `config.json` to the worker, runs the
script, and brings `result_$(ProcId).json` back. 100 of these run in
parallel across the pool.

**Important:** Create the `logs/` directory before submitting.

### Parameter sweeps

Read parameters from a file:

```
executable = /usr/bin/python3
arguments  = train.py --lr $(lr) --batch $(batch) --seed $(seed)
transfer_input_files = train.py
output = sweep/$(ClusterId).$(ProcId).out
error  = sweep/$(ClusterId).$(ProcId).err
log    = sweep.log
should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
request_cpus   = 1
request_memory = 4G

queue lr, batch, seed from params.txt
```

```
# params.txt
0.001 32 42
0.001 64 42
0.01  32 42
0.01  64 42
```

Or inline:

```
queue lr batch seed in (
  0.001 32 42
  0.001 64 42
  0.01  32 42
  0.01  64 42
)
```

### C++ binaries

```
executable = my_program
arguments  = --headless --games 10
transfer_input_files    = my_program
should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
output = out/$(ClusterId).$(ProcId).out
error  = out/$(ClusterId).$(ProcId).err
log    = eval.log
request_cpus   = 1
request_memory = 1G
queue 50
```

Compile your binary on any lab machine (Ubuntu 22.04, GLIBC 2.35). The
binary gets transferred to each worker automatically.

---

## Container Jobs

If you need packages that aren't installed on the lab machines, use a
container image from the local registry. No need to install anything on
workers yourself.

```
universe        = container
container_image = 136.244.224.136:5000/aall/base:latest
executable      = my_script.py
arguments       = --input data.json

transfer_input_files    = my_script.py, data.json
should_transfer_files   = YES
when_to_transfer_output = ON_EXIT

output = out/$(ClusterId).$(ProcId).out
error  = out/$(ClusterId).$(ProcId).err
log    = condor.log

request_cpus   = 1
request_memory = 2G

queue 100
```

### Available images

| Image | What's in it |
|-------|-------------|
| `aall/base` | Python 3.10, numpy, scipy, pandas, matplotlib, sklearn |
| `aall/base-cuda` | Same as base plus PyTorch and CUDA 12.4 |
| `aall/nethack` | NetHack Learning Environment |
| `aall/fightingice` | DareFightingICE, Java runtime |
| `aall/dragonchess` | Dragonchess engine, PyTorch |
| `aall/qdavc` | QD variable constraints framework |

Images are pulled automatically on first use and cached on each worker.

### Building your own image

If your project needs a custom environment, ask Jim to add a Dockerfile
to the registry, or build one yourself on mega_knight:

```bash
# On mega_knight
mkdir my_project && cd my_project

cat > Dockerfile <<'DOCKERFILE'
FROM 136.244.224.136:5000/aall/base:latest
RUN python3 -m pip install --no-cache-dir torch gymnasium
COPY my_code/ /workspace/
ENV PYTHONPATH=/workspace
WORKDIR /workspace
DOCKERFILE

docker build -t 136.244.224.136:5000/aall/my_project:latest .
docker push 136.244.224.136:5000/aall/my_project:latest
```

Then use `container_image = 136.244.224.136:5000/aall/my_project:latest`
in your submit file.

---

## GPU Jobs

Two machines have RTX 5090 GPUs (threadripper and vertex).

```
request_gpus = 1
requirements = (CUDACapability >= 7.5)
```

Condor routes GPU jobs to the right machines and sets
`CUDA_VISIBLE_DEVICES` automatically.

Use a CUDA container image for GPU jobs:

```
universe        = container
container_image = 136.244.224.136:5000/aall/base-cuda:latest
executable      = train.py
request_gpus    = 1
requirements    = (CUDACapability >= 7.5)
```

GPU machines are shared. Keep GPU jobs short or ask Jim before running
long training sessions.

---

## Targeting Machines

```
requirements = (AALL_TIER == "research")               # NLH 209 only
requirements = (AALL_TIER =!= "teaching")              # skip student desktops
requirements = (TotalSlotCpus >= 32)                    # big machines only
```

Machine tiers:

| Tier | Room | Machines | Policy |
|------|------|----------|--------|
| teaching | NLH 214 | 18 | Jobs run only when desktop is idle |
| server | NLH 210 | 20 | Always running |
| research | NLH 209 | 9 | Always running |

Teaching machines automatically suspend your job when a student uses the
keyboard, and resume it when they leave. If the student stays for 5
minutes, the job gets killed and rescheduled elsewhere. Your code should
be able to handle this (checkpoint periodically).

---

## Useful Commands

```bash
condor_q                          # your jobs
condor_q -allusers                # everyone's jobs
condor_q -held -af HoldReason     # why a job is stuck
condor_q -run                     # which machine each job is on
condor_history -limit 20          # recently finished jobs
condor_status                     # all machines in the pool
condor_status -compact            # one-line-per-machine summary
condor_status -gpus               # GPU machines

condor_rm <cluster_id>            # kill a job cluster
condor_rm -all                    # kill all your jobs
condor_hold <cluster_id>          # pause a job
condor_release <cluster_id>       # unpause a job
```

---

## Troubleshooting

### Job is held

```bash
condor_q -held -af HoldReason
```

Common causes:
- **Missing file:** You forgot to list a file in `transfer_input_files`.
  Every file your code needs must be listed there.
- **Permission denied:** Your binary isn't executable. Run `chmod +x` on
  it before submitting.
- **Missing output directory:** Create directories like `logs/` and
  `out/` before submitting. Condor doesn't create them for you.
- **Out of memory:** Your job used more memory than `request_memory`.
  Increase the request.

### Wrong results

- Jobs run in a temporary sandbox on the worker. **All paths are
  relative.** Don't hardcode absolute paths like `/home/you/data.csv`.
  Transfer everything you need.
- If your code reads from stdin, add `input = /dev/null` to avoid hangs.

### Job runs on the wrong machine

Add a `requirements` line to your submit file. See "Targeting Machines"
above.

### Job keeps getting evicted

If your job is running on a teaching machine and keeps getting killed
when students arrive, add:

```
requirements = (AALL_TIER =!= "teaching")
```

This restricts your job to the always-on machines.

### Can't connect to mega_knight

Make sure you're on campus or connected to the VPN. The cluster is on
the campus internal network (136.244.x.x) and not reachable from the
public internet.

---

## Rules

1. **Don't run jobs that modify the worker machine.** No `apt install`,
   no writing outside your sandbox, no changing system settings.
2. **Don't monopolize GPUs.** Two GPUs serve the whole lab. Keep GPU
   jobs short, or coordinate with Jim and other researchers.
3. **Clean up after yourself.** Remove old output files and logs when
   you're done. Disk space on mega_knight is shared.
4. **Checkpoint long jobs.** Teaching machines will evict your job when
   students arrive. Research machines are more stable but not guaranteed.
   Write checkpoints so you can resume.
5. **Ask Jim** if you're unsure about anything or need a custom container
   image built.

---

## Contact

Jim O'Connor, joconno2@conncoll.edu, NLH 217
