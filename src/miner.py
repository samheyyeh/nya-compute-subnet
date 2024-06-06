import argparse
import logging
import pandas as pd
import time
import torch
import uvicorn
from communex.compat.key import classic_load_key
from communex.module import endpoint
from communex.module.module import Module
from communex.module.server import ModuleServer
from datasets import Dataset
from keylimiter import TokenBucketLimiter
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModelForMaskedLM

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info(f"Running {__file__}")


class NyaComputeMiner(Module):
    """
    A module class for mining and generating responses to prompts.

    Attributes:
        None

    Methods:
        generate: Generates a response to a given prompt using a specified model.
    """

    def __init__(self,
                 batch_size: int = 64,
                 device: str = "cuda",
                 # store_tasks: bool = False,
                 ):
        super().__init__()
        model_name = "distilbert/distilbert-base-uncased"

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForMaskedLM.from_pretrained(model_name)
        self.batch_size = batch_size
        # self.store_tasks = store_tasks

        if device == "cuda" and not torch.cuda.is_available():
            logger.error("CUDA is not available. aborting.")
            raise ValueError("CUDA is not available.")

        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.model = self.model.to(self.device)  # .half()
        self.model.eval()

        logger.info(f"Initialized {self.__class__.__name__}, using device: {self.device}")

    def batch_encode(self, batch):
        return self.tokenizer(batch["text"],
                              padding="max_length",
                              truncation=True,
                              return_tensors="pt")

    @endpoint
    def compute(self, task: list[str]):
        start_time = time.perf_counter()
        logger.debug(f"Received a new task with {len(task)} items.")

        result = {}
        task_dict_list = [{"text": t} for t in task]

        input_data = Dataset.from_list(task_dict_list)

        encoded = input_data.map(self.batch_encode,
                                 batched=True,
                                 remove_columns=["text"],
                                 batch_size=64
                                 )

        # TODO: optimize for multi-GPU environments
        # TODO: calculate the optimal batch size for the model

        data_loader = DataLoader(encoded, batch_size=64)
        logger.debug(f"Data loaded in {time.perf_counter() - start_time:.2f} seconds")
        # last_hidden_states = []
        logit_list = []
        logit_index_list = []
        logger.debug(f"Starting forward pass...")
        with torch.no_grad():
            for batch in data_loader:
                batch = {k: torch.stack(batch["input_ids"], dim=1).to(self.device) for k, v in batch.items()}

                output = self.model(**batch,
                                    output_hidden_states=True,
                                    return_dict=True
                                    )

                top_k_logit, top_k_indices = torch.topk(output.logits, 16, dim=-1)
                logit_list.append(top_k_logit)
                logit_index_list.append(top_k_indices)
        logger.debug(f"Forward pass completed in {time.perf_counter() - start_time:.2f} seconds")

        logit = torch.cat(logit_list, dim=0).to(torch.float16)
        logit_index = torch.cat(logit_index_list, dim=0).to(torch.int16)
        end_time = time.perf_counter()

        elapsed_time = end_time - start_time

        # TODO: numpy().tobytes() is preferred over numpy().tolist() for performance reasons
        # TODO: however, transferring bytes causes a serialization error

        # last_hidden_states_bytes = [[l.numpy().tobytes() for l in batch] for batch in last_hidden_states]
        result["elapsed_time"] = elapsed_time
        result["logit"] = logit.numpy().tolist()
        result["logit_index"] = logit_index.numpy().tolist()
        logger.debug(f"Compute task completed in {elapsed_time:.2f} seconds")

        # TODO: must run in a separate thread to avoid delaying the response
        # if self.store_tasks:
        #     current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        #     data_df = pd.DataFrame(task, columns=["text"])
        #     data_df.to_csv(f"task_{current_time_str}.csv", index=False)

        return result


def main():
    parser = argparse.ArgumentParser(description="Load validator configuration.")
    parser.add_argument("--name", help="Miner name.", default="nya compute miner")
    parser.add_argument("--keyfile", help="Name of the key file", default="nya-miner")
    parser.add_argument("--ip", help="IP address to bind the server to.", default="0.0.0.0")
    parser.add_argument("--port", help="Port to bind the server to.", default=9910)
    parser.add_argument("--device", help="Device to run the model on.", default="cuda")
    parser.add_argument("--batch_size", help="Batch size for the model.", default=64)

    parser.add_argument("--subnetuid", help="Subnet UID to bind the server to.", default=23)

    # TODO: potentially implement this feature for logging purposes
    # TODO: ensure saving data occurs in a separate thread to avoid delaying the response
    # parser.add_argument("--store_tasks", help="Store tasks in CSV format.", default=False)

    args = parser.parse_args()

    port = args.port

    if isinstance(port, str) and port.isdigit():
        port = int(port)
    else:
        logger.error("Port must be an integer. aborting.")
        raise ValueError("Port must be an integer.")

    # key = generate_keypair()

    try:
        key = classic_load_key(args.keyfile)
    except FileNotFoundError:
        logger.error(f"Key file {args.keyfile} not found. aborting.")
        raise FileNotFoundError(f"Key file {args.keyfile} not found.")

    miner = NyaComputeMiner(
        batch_size=args.batch_size,
        device=args.device,
        # store_tasks=args.store_tasks
    )
    refill_rate = 1 #

    # Implementing custom limit
    # TODO: investigate the impact of TokenBucketLimiter
    bucket = TokenBucketLimiter(30, refill_rate)

    use_testnet = args.subnetuid == 23

    if use_testnet:
        logger.info("Using testnet")

    server = ModuleServer(miner,
                          key,
                          limiter=bucket,
                          subnets_whitelist=[args.subnetuid],
                          use_testnet=use_testnet)
    app = server.get_fastapi_app()

    uvicorn.run(app, host=args.ip, port=port)


if __name__ == "__main__":
    main()
