#!/usr/bin/env bash

echo "$BUILDKITE_PARALLEL_JOB"
echo "$BUILDKITE_PARALLEL_JOB_COUNT"

set -euox pipefail

function finish {
    rm -rf $(find /workdir -group root)
}
trap finish EXIT

SYNTHETIC_SCRIPT="/bagua/examples/benchmark/synthetic_benchmark.py"

function check_benchmark_log {
    logfile=$1

    final_img_per_sec=$(cat ${logfile} | grep "Img/sec per " | tail -n 1 | awk '{print $4}')
    threshold="70.0"

    if [[ $final_img_per_sec -le $threshold ]]; then
        exit 1
    fi
}
export HOME=/workdir

cd /workdir && pip install .
rm -rf /workdir/.cargo
curl https://sh.rustup.rs -sSf | sh
pip install git+https://github.com/BaguaSys/bagua-core@master

logfile=$(mktemp /tmp/bagua_benchmark.XXXXXX.log)
python -m bagua.distributed.run \
    --standalone \
    --nnodes=1 \
    --nproc_per_node 4 \
    --no_python \
    --autotune_level 1 \
    --default_bucket_size 2147483648 \
    --autotune_warmup_time 10 \
    --autotune_max_samples 30 \
    python ${SYNTHETIC_SCRIPT} \
        --num-iters 200 \
        --model vgg16 \
        2>&1 | tee ${logfile}
check_benchmark_log ${logfile}
