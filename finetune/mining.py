# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import time
import traceback
from dataclasses import replace
from typing import Any, Dict, Optional

import bittensor as bt
import huggingface_hub
import taoverse.utilities.logging as logging
from taoverse.model import utils as model_utils
from taoverse.model.data import Model, ModelId
from taoverse.model.storage.chain.chain_model_metadata_store import (
    ChainModelMetadataStore,
)
from taoverse.model.storage.hugging_face.hugging_face_model_store import (
    HuggingFaceModelStore,
)
from taoverse.model.storage.model_metadata_store import ModelMetadataStore
from taoverse.model.storage.remote_model_store import RemoteModelStore
from taoverse.model.utils import get_hash_of_two_strings
from transformers import AutoModelForCausalLM, AutoTokenizer

import constants
import finetune as ft
from competitions.data import CompetitionId


def model_path(base_dir: str, run_id: str) -> str:
    """
    Constructs a file path for storing the model relating to a training run.
    """
    return os.path.join(base_dir, "training", run_id)


async def push(
    model: Model,
    repo: str,
    competition_id: CompetitionId,
    wallet: bt.wallet,
    retry_delay_secs: int = 60,
    update_repo_visibility: bool = False,
    metadata_store: Optional[ModelMetadataStore] = None,
    remote_model_store: Optional[RemoteModelStore] = None,
):
    """Pushes the model to Hugging Face and publishes it on the chain for evaluation by validators.

    Args:
        model (Model): The model to push. ModelId is overwritten based on the other parameters.
        repo (str): The repo to push to. Must be in format "namespace/name".
        competition_id (CompetitionId): The competition the miner is participating in.
        wallet (bt.wallet): The wallet of the Miner uploading the model.
        retry_delay_secs (int): The number of seconds to wait before retrying to push the model to the chain.
        update_repo_visibility (bool): Whether to make the repo public after pushing the model.
        metadata_store (Optional[ModelMetadataStore]): The metadata store. If None, defaults to writing to the
            chain.
        remote_model_store (Optional[RemoteModelStore]): The remote model store. If None, defaults to writing to HuggingFace
    """
    logging.info("Pushing model")

    subtensor = bt.subtensor()
    subnet_uid = constants.SUBNET_UID

    if metadata_store is None:
        metadata_store = ChainModelMetadataStore(
            subtensor=subtensor, subnet_uid=subnet_uid, wallet=wallet
        )

    if remote_model_store is None:
        remote_model_store = HuggingFaceModelStore()

    model_constraints = constants.MODEL_CONSTRAINTS_BY_COMPETITION_ID.get(
        competition_id, None
    )
    if not model_constraints:
        raise ValueError("Invalid competition_id")

    # First upload the model to HuggingFace.
    namespace, name = model_utils.validate_hf_repo_id(repo)
    # Overwrite the model id with the current information.
    model.id = ModelId(namespace=namespace, name=name, competition_id=competition_id)
    # Get the new model id which includes hash information.
    model_id_with_hash = await remote_model_store.upload_model(model, model_constraints)

    logging.info("Uploaded model to hugging face.")

    secure_hash = get_hash_of_two_strings(
        model_id_with_hash.hash, wallet.hotkey.ss58_address
    )
    model_id_with_hash = replace(model_id_with_hash, secure_hash=secure_hash)

    logging.info(f"Now committing to the chain with model_id: {model_id_with_hash}")

    # We can only commit to the chain every 20 minutes, so run this in a loop, until
    # successful.
    while True:
        try:
            await metadata_store.store_model_metadata(
                wallet.hotkey.ss58_address, model_id_with_hash
            )

            logging.info(
                "Wrote model metadata to the chain. Checking we can read it back..."
            )

            logging.debug(
                "Retrieving model's UID..."
            )

            uid = subtensor.get_uid_for_hotkey_on_subnet(wallet.hotkey.ss58_address, subnet_uid)

            model_metadata = await metadata_store.retrieve_model_metadata(
                uid, wallet.hotkey.ss58_address
            )

            if (
                not model_metadata
                or model_metadata.id.to_compressed_str()
                != model_id_with_hash.to_compressed_str()
            ):
                logging.error(
                    f"Failed to read back model metadata from the chain. Expected: {model_id_with_hash}, got: {model_metadata}"
                )
                raise ValueError(
                    f"Failed to read back model metadata from the chain. Expected: {model_id_with_hash}, got: {model_metadata}"
                )

            logging.info("Committed model to the chain.")
            break
        except Exception as e:
            logging.error(
                f"Failed to advertise model on the chain: {traceback.format_exc()}"
            )
            logging.error(f"Retrying in {retry_delay_secs} seconds...")
            time.sleep(retry_delay_secs)

    if update_repo_visibility:
        logging.debug("Making repo public.")
        huggingface_hub.update_repo_visibility(
            repo,
            private=False,
            token=HuggingFaceModelStore.assert_access_token_exists(),
        )
        logging.info("Model set to public")


def save(model: Model, model_dir: str):
    """Saves a model to the provided directory"""
    if not os.path.exists(model_dir):
        os.makedirs(model_dir, exist_ok=True)

    # Save the model state to the specified path.
    model.pt_model.save_pretrained(
        save_directory=model_dir,
        safe_serialization=True,
    )

    if model.tokenizer is not None:
        model.tokenizer.save_pretrained(
            save_directory=model_dir,
            safe_serialization=True,
        )


async def get_repo(
    uid: int,
    metagraph: Optional[bt.metagraph] = None,
    metadata_store: Optional[ModelMetadataStore] = None,
) -> str:
    """Returns a URL to the HuggingFace repo of the Miner with the given UID."""
    if metadata_store is None:
        metadata_store = ChainModelMetadataStore(
            subtensor=bt.subtensor(), subnet_uid=constants.SUBNET_UID
        )
    if metagraph is None:
        metagraph = bt.metagraph(netuid=constants.SUBNET_UID)

    hotkey = metagraph.hotkeys[uid]
    model_metadata = await metadata_store.retrieve_model_metadata(uid, hotkey)

    if not model_metadata:
        raise ValueError(f"No model metadata found for miner {uid}")

    return model_utils.get_hf_url(model_metadata)


def load_local_model(
    model_dir: str, competition_id: CompetitionId, kwargs: Dict[str, Any]
) -> Model:
    """Loads a model from a directory."""
    model_id = ModelId(
        namespace="local_namespace",
        name="local_model",
        competition_id=competition_id,
    )

    pt_model = AutoModelForCausalLM.from_pretrained(
        pretrained_model_name_or_path=model_dir,
        local_files_only=True,
        use_safetensors=True,
        **kwargs,
    )

    tokenizer = None
    if competition_id == CompetitionId.INSTRUCT_8B:
        # Do not use the kwargs for the model load here. If needed in the future a separate kwargs can be plumbed.
        # This may throw an exception if no model is found.
        tokenizer = AutoTokenizer.from_pretrained(
            pretrained_model_name_or_path=model_dir,
            local_files_only=True,
            use_safetensors=True,
        )

    return Model(id=model_id, pt_model=pt_model, tokenizer=tokenizer)


async def load_remote_model(
    uid: int,
    download_dir: str,
    metagraph: Optional[bt.metagraph] = None,
    metadata_store: Optional[ModelMetadataStore] = None,
    remote_model_store: Optional[RemoteModelStore] = None,
) -> Model:
    """Loads the model currently being advertised by the Miner with the given UID.

    Args:
        uid (int): The UID of the Miner who's model should be downloaded.
        download_dir (str): The directory to download the model to.
        metagraph (Optional[bt.metagraph]): The metagraph of the subnet.
        metadata_store (Optional[ModelMetadataStore]): The metadata store. If None, defaults to reading from the
        remote_model_store (Optional[RemoteModelStore]): The remote model store. If None, defaults to reading from HuggingFace
    """

    if metagraph is None:
        metagraph = bt.metagraph(netuid=constants.SUBNET_UID)

    if metadata_store is None:
        metadata_store = ChainModelMetadataStore(
            subtensor=bt.subtensor(), subnet_uid=constants.SUBNET_UID
        )

    if remote_model_store is None:
        remote_model_store = HuggingFaceModelStore()

    hotkey = metagraph.hotkeys[uid]
    model_metadata = await metadata_store.retrieve_model_metadata(uid, hotkey)
    if not model_metadata:
        raise ValueError(f"No model metadata found for miner {uid}")

    model_constraints = constants.MODEL_CONSTRAINTS_BY_COMPETITION_ID.get(
        model_metadata.id.competition_id, None
    )

    if not model_constraints:
        raise ValueError("Invalid competition_id")

    logging.info(f"Fetched model metadata: {model_metadata}")
    model: Model = await remote_model_store.download_model(
        model_metadata.id, download_dir, model_constraints
    )
    return model


async def load_best_model(
    download_dir: str,
    competition_id: CompetitionId,
    metagraph: Optional[bt.metagraph] = None,
    metadata_store: Optional[ModelMetadataStore] = None,
    remote_model_store: Optional[RemoteModelStore] = None,
) -> Model:
    """Loads the model from the best performing miner to download_dir"""
    best_uid = ft.graph.best_uid(competition_id=competition_id)
    if best_uid is None:
        raise ValueError(f"No best models found for {competition_id}")

    return await load_remote_model(
        best_uid,
        download_dir,
        metagraph,
        metadata_store,
        remote_model_store,
    )
