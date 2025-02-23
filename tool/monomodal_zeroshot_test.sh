#!/bin/bash

## uncomment for slurm
##SBATCH -p quadro
##SBATCH --gres=gpu:1
##SBATCH -c 10

export PYTHONPATH=./
eval "$(conda shell.bash hook)"
conda activate torchEnv  # pytorch env
PYTHON=python

dataset=$1
exp_name=$2
exp_dir=exp/${dataset}/${exp_name}
model_dir=${exp_dir}/model
result_dir=${exp_dir}/result
config=config/${dataset}/${dataset}_${exp_name}.yaml
now=$(date +"%Y%m%d_%H%M%S")

cp tool/monomodal_zeroshot_test.sh tool/monomodal_zeroshot_test.py ${config} ${exp_dir}

export PYTHONPATH=./
$PYTHON -u ${exp_dir}/monomodal_zeroshot_test.py \
  --config=${config} \
  2>&1 | tee ${result_dir}/monomodal_zeroshot_test-$now.log
