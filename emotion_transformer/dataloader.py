import logging

logging.disable(logging.CRITICAL)
import torch
import pandas as pd
from transformers import DistilBertTokenizer
from torch.utils.data import TensorDataset, DataLoader
from torch.utils.data.distributed import DistributedSampler


def open_data(path):
    """
    returns a Pandas DataFrame consisting of the SemEval data at path `path`
    """
    return pd.read_csv(path, delimiter="\t", index_col="id")


def transform_data(df, max_seq_len):
    """
    returns the padded input ids and attention masks according to the DistilBert tokenizer
    """
    tokenizer = DistilBertTokenizer.from_pretrained("distilbert-base-uncased")

    def tokenize_fct(turn):
        return tokenizer.encode(turn, add_special_tokens=True, max_length=max_seq_len)

    tokenized = df[["turn1", "turn2", "turn3"]].applymap(tokenize_fct)
    padded = torch.tensor(
        [
            [ids + [0] * (max_seq_len - len(ids)) for ids in idx]
            for idx in tokenized.values
        ]
    )
    attention_mask = torch.where(
        padded != 0, torch.ones_like(padded), torch.zeros_like(padded)
    )
    return padded, attention_mask


def get_labels(df, emo_dict):
    """
    returns the labels according to the emotion dictionary
    """
    return torch.tensor([emo_dict[label] for label in df["label"].values])


def dataloader(path, max_seq_len, batch_size, emo_dict, use_ddp=False, labels=True):
    """
    Transforms the .csv data stored in `path` according to DistilBert features and returns it as a DataLoader
    """
    df = open_data(path)
    padded, attention_mask = transform_data(df, max_seq_len)

    if labels:
        dataset = TensorDataset(padded, attention_mask, get_labels(df, emo_dict))

    else:
        dataset = TensorDataset(padded, attention_mask)

    if use_ddp:
        train_sampler = DistributedSampler(dataset)

    else:
        train_sampler = None

    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=(train_sampler is None),
        sampler=train_sampler,
        num_workers=3,
    )
