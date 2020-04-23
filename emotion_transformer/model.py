import logging

logging.disable(logging.CRITICAL)
import torch
from transformers import (
    DistilBertModel,
    DistilBertForSequenceClassification,
    DistilBertConfig,
)
from .dataloader import dataloader


class sentence_embeds_model(torch.nn.Module):
    """
    instantiates the pretrained DistilBert model and the linear layer
    """

    def __init__(self, dropout=0.1):
        super(sentence_embeds_model, self).__init__()

        self.transformer = DistilBertModel.from_pretrained(
            "distilbert-base-uncased", dropout=dropout, output_hidden_states=True
        )
        self.embedding_size = 2 * self.transformer.config.hidden_size

    def layerwise_lr(self, lr, decay):
        """
        returns grouped model parameters with layer-wise decaying learning rate
        """
        bert = self.transformer
        num_layers = bert.config.n_layers
        opt_parameters = [
            {"params": bert.embeddings.parameters(), "lr": lr * decay ** num_layers}
        ]
        opt_parameters += [
            {
                "params": bert.transformer.layer[l].parameters(),
                "lr": lr * decay ** (num_layers - l + 1),
            }
            for l in range(num_layers)
        ]
        return opt_parameters

    def forward(self, input_ids=None, attention_mask=None, input_embeds=None):
        """
        returns the sentence embeddings
        """
        if input_ids is not None:
            input_ids = input_ids.flatten(end_dim=1)
        if attention_mask is not None:
            attention_mask = attention_mask.flatten(end_dim=1)
        output = self.transformer(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=input_embeds,
        )

        cls = output[0][:, 0]
        hidden_mean = torch.mean(output[1][-1], 1)
        sentence_embeds = torch.cat([cls, hidden_mean], dim=-1)

        return sentence_embeds.view(-1, 3, self.embedding_size)


class context_classifier_model(torch.nn.Module):
    """
    instantiates the DisitlBertForSequenceClassification model, the position embeddings of the utterances,
    and the binary loss function
    """

    def __init__(
        self, embedding_size, projection_size, n_layers, emo_dict, dropout=0.1
    ):
        super(context_classifier_model, self).__init__()

        self.projection_size = projection_size
        self.projection = torch.nn.Linear(embedding_size, projection_size)
        self.position_embeds = torch.nn.Embedding(3, projection_size)
        self.norm = torch.nn.LayerNorm(projection_size)
        self.drop = torch.nn.Dropout(dropout)

        context_config = DistilBertConfig(
            dropout=dropout,
            dim=projection_size,
            hidden_dim=4 * projection_size,
            n_layers=n_layers,
            n_heads=1,
            num_labels=4,
        )

        self.context_transformer = DistilBertForSequenceClassification(context_config)
        self.others_label = emo_dict["others"]
        self.bin_loss_fct = torch.nn.BCEWithLogitsLoss()

    def bin_loss(self, logits, labels):
        """
        defined the additional binary loss for the `others` label
        """
        bin_labels = torch.where(
            labels == self.others_label,
            torch.ones_like(labels),
            torch.zeros_like(labels),
        ).float()
        bin_logits = logits[:, self.others_label]
        return self.bin_loss_fct(bin_logits, bin_labels)

    def forward(self, sentence_embeds, labels=None):
        """
        returns the logits and the corresponding loss if `labels` are given
        """

        position_ids = torch.arange(3, dtype=torch.long, device=sentence_embeds.device)
        position_ids = position_ids.expand(sentence_embeds.shape[:2])
        position_embeds = self.position_embeds(position_ids)
        sentence_embeds = self.projection(sentence_embeds) + position_embeds
        sentence_embeds = self.drop(self.norm(sentence_embeds))
        if labels is None:
            return self.context_transformer(
                inputs_embeds=sentence_embeds.flip(1), labels=labels
            )[0]

        else:
            loss, logits = self.context_transformer(
                inputs_embeds=sentence_embeds.flip(1), labels=labels
            )
            return loss + self.bin_loss(logits, labels), logits


def metrics(loss, logits, labels):
    cm = torch.zeros((4, 4), device=loss.device)
    preds = torch.argmax(logits, dim=1)
    acc = (labels == preds).float().mean()
    for label, pred in zip(labels.view(-1), preds.view(-1)):
        cm[label.long(), pred.long()] += 1

    tp = cm.diagonal()[1:].sum()
    fp = cm[:, 1:].sum() - tp
    fn = cm[1:, :].sum() - tp
    return {"val_loss": loss, "val_acc": acc, "tp": tp, "fp": fp, "fn": fn}


def f1_score(tp, fp, fn):
    prec_rec_f1 = {}
    prec_rec_f1["precision"] = tp / (tp + fp)
    prec_rec_f1["recall"] = tp / (tp + fn)
    prec_rec_f1["f1_score"] = (
        2
        * (prec_rec_f1["precision"] * prec_rec_f1["recall"])
        / (prec_rec_f1["precision"] + prec_rec_f1["recall"])
    )
    return prec_rec_f1
