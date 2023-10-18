#!/bin/bash
#SBATCH --mem=32G
#SBATCH --nodes=1
#SBATCH --time=0:20:0  
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:a100:2

#### SBATCH --mem=16G
#### SBATCH --nodes=1
#### SBATCH --time=0:10:0  
#### SBATCH --ntasks-per-node=8
#### SBATCH --gres=gpu:a100:1

#### SBATCH --mail-user=<youremail@gmail.com>
#### SBATCH --mail-type=ALL

module load StdEnv/2020  # CUDA etc
nvidia-smi

# PYTHON=/home/walml/envs/zoobot39_dev/bin/python

module load python/3.9.6
virtualenv --no-download $SLURM_TMPDIR/env
source $SLURM_TMPDIR/env/bin/activate
pip install --no-index -r /project/def-bovy/walml/zoobot/only_for_me/narval/requirements.txt
cp -r /project/def-bovy/walml/galaxy-datasets $SLURM_TMPDIR/
cp -r /project/def-bovy/walml/zoobot $SLURM_TMPDIR/
pip install --no-deps -e $SLURM_TMPDIR/galaxy-datasets
pip install --no-deps -e $SLURM_TMPDIR/zoobot

mkdir $SLURM_TMPDIR/walml
mkdir $SLURM_TMPDIR/walml/finetune
mkdir $SLURM_TMPDIR/walml/finetune/data
mkdir $SLURM_TMPDIR/walml/finetune/checkpoints

cp -r /project/def-bovy/walml/data/roots/galaxy_mnist $SLURM_TMPDIR/walml/finetune/data/

ls $SLURM_TMPDIR/walml/finetune/data/galaxy_mnist

pip install --no-index wandb

wandb offline  # only write metadata locally

# $PYTHON /project/def-bovy/walml/zoobot/only_for_me/narval/finetune.py
python $SLURM_TMPDIR/zoobot/only_for_me/narval/finetune.py

ls $SLURM_TMPDIR/walml/finetune/checkpoints
