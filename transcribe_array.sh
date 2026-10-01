#!/bin/bash -l
#SBATCH --job-name=CanaluTranscription
#SBATCH --output=logs/transcribe-%A_%a.out
#SBATCH --error=logs/transcribe-%A_%a.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --mem=32G
#SBATCH --time=10-00:00:00
#SBATCH --partition=gpu
#SBATCH --constraint=GPURAM_Min_16GB
#SBATCH --gpus-per-node=1
#SBATCH --array=0-47%4

set -euo pipefail

# Slurm copie ce script dans son spool : son emplacement n'est plus le projet.
# launch_transcriptions.py définit le répertoire de travail avec sbatch --chdir.
if [[ -n "${SLURM_JOB_ID:-}" ]]; then
  PROJECT_DIR="$PWD"
else
  PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
fi
cd "$PROJECT_DIR"
source .venv-whisper/bin/activate

export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"

# On peut fournir un autre dossier de plan en premier argument de sbatch.
PLAN_DIR="${1:-transcription_batches}"

if [[ ! -f "$PLAN_DIR/plan.json" ]]; then
  echo "Plan introuvable : $(realpath -m -- "$PLAN_DIR")/plan.json" >&2
  echo "Lancer d'abord launch_transcriptions.py, ou transcribe_slurm.py prepare." >&2
  exit 1
fi

echo "Lot ${SLURM_ARRAY_TASK_ID} ; GPU visibles : ${CUDA_VISIBLE_DEVICES:-non définis}"
srun python -u transcribe_slurm.py run \
  --plan "$PLAN_DIR" \
  --shard-index "$SLURM_ARRAY_TASK_ID" \
  --device cuda
