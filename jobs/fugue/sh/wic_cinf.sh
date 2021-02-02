#!/bin/bash
#SBATCH --job-name=wic_cinf
#SBATCH --output=/extra/ucinlp0/rlogan/fugue_wic_cinf.txt
#SBATCH --time=1000:00
#SBATCH --partition=ava_s.p
#SBATCH --nodelist=ava-s3
#SBATCH --gpus=8
#SBATCH --cpus-per-task=32
#SBATCH --mem=400GB

srun /home/rlogan/miniconda3/envs/autoprompt/bin/python3.7 \
    scripts/launch.py \
    --logdir results/wic \
    jobs/fugue/yaml/wic_cinf.yaml
