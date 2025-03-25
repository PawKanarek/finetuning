from typing import Any, Dict, Set

from finetune.datasets.generated.dyck_loader import DyckLoader
from finetune.datasets.generated.if_eval_loader import IFEvalLoader
from finetune.datasets.generated.word_sorting_loader import WordSortingLoader
from finetune.datasets.hugging_face.hugging_face_loader import (
    FINEWEB_EDU_SCORE_2_NAME,
    HuggingFaceLoader,
    Synthetic1SFTLoader,
    CodeforcesCOTSLoader,
)
from finetune.datasets.ids import DatasetId
from finetune.datasets.loader import DatasetLoader


class DatasetLoaderFactory:
    @staticmethod
    def get_loader(
        dataset_id: DatasetId,
        dataset_kwargs: Dict[str, Any],
        seed: int,
        validator_hotkeys: Set[str],
    ) -> DatasetLoader:
        """Loads data samples from the appropriate dataset."""

        match dataset_id:
            case DatasetId.DYCK_LANGUAGE:
                return DyckLoader(random_seed=seed, **dataset_kwargs)
            case DatasetId.SYNTHETIC_MMLU:
                raise NotImplementedError(
                    "Prompting dataset is not implemented and should be loaded elsewhere."
                )
            case DatasetId.WORD_SORTING:
                return WordSortingLoader(random_seed=seed, **dataset_kwargs)
            case DatasetId.FINEWEB:
                return HuggingFaceLoader(
                    name=FINEWEB_EDU_SCORE_2_NAME, random_seed=seed, **dataset_kwargs
                )
            case DatasetId.SYNTHETIC_IF_EVAL:
                return IFEvalLoader(
                    random_seed=seed,
                    validator_hotkeys=validator_hotkeys,
                    **dataset_kwargs,
                )
            case DatasetId.SYNTHETIC_1_SFT:
                return Synthetic1SFTLoader(
                    random_seed=seed,
                    **dataset_kwargs,
                )
            case DatasetId.CODEFORCES_COTS:
                return CodeforcesCOTSLoader(
                    random_seed=seed,
                    **dataset_kwargs,
                )
            case _:
                raise ValueError(f"Unknown dataset_id: {dataset_id}")
