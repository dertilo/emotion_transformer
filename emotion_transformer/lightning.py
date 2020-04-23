import logging

logging.disable(logging.CRITICAL)
import os
import random
import torch
import pytorch_lightning as pl
from test_tube import HyperOptArgumentParser
from .dataloader import dataloader
from .model import sentence_embeds_model, context_classifier_model, metrics, f1_score
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from pytorch_lightning.loggers.mlflow import MLFlowLogger


class EmotionModel(pl.LightningModule):
    """
    PyTorch Lightning module for the Contextual Emotion Detection in Text Challenge
    """

    def __init__(self, hparams):
        """
        pass in parsed HyperOptArgumentParser to the model
        """
        super(EmotionModel, self).__init__()
        self.hparams = hparams
        self.emo_dict = {"others": 0, "sad": 1, "angry": 2, "happy": 3}
        self.sentence_embeds_model = sentence_embeds_model(dropout=hparams.dropout)
        self.context_classifier_model = context_classifier_model(
            self.sentence_embeds_model.embedding_size,
            hparams.projection_size,
            hparams.n_layers,
            self.emo_dict,
            dropout=hparams.dropout,
        )

    def forward(self, input_ids, attention_mask, labels=None):
        """
        no special modification required for lightning, define as you normally would
        """
        if self.current_epoch < self.hparams.frozen_epochs:
            with torch.no_grad():
                sentence_embeds = self.sentence_embeds_model(
                    input_ids=input_ids, attention_mask=attention_mask
                )
        else:
            sentence_embeds = self.sentence_embeds_model(
                input_ids=input_ids, attention_mask=attention_mask
            )
        return self.context_classifier_model(
            sentence_embeds=sentence_embeds, labels=labels
        )

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop
        """
        input_ids, attention_mask, labels = batch
        loss, _ = self.forward(
            input_ids=input_ids, attention_mask=attention_mask, labels=labels
        )
        # in DP mode (default) make sure if result is scalar, there's another dim in the beginning
        if self.trainer.use_dp or self.trainer.use_ddp2:
            loss = loss.unsqueeze(0)

        tensorboard_logs = {"train_loss": loss}
        return {"loss": loss, "log": tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop
        """
        input_ids, attention_mask, labels = batch

        loss, logits = self.forward(
            input_ids=input_ids, attention_mask=attention_mask, labels=labels
        )
        scores_dict = metrics(loss, logits, labels)

        # in DP mode (default) make sure if result is scalar, there's another dim in the beginning
        if self.trainer.use_dp or self.trainer.use_ddp2:
            scores = [score.unsqueeze(0) for score in scores_dict.values()]
            scores_dict = {key: value for key, value in zip(scores_dict.keys(), scores)}

        return scores_dict

    def validation_end(self, outputs):
        """
        called at the end of validation to aggregate outputs
        :param outputs: list of individual outputs of each validation step
        :return:
        """

        tqdm_dict = {}

        for metric_name in outputs[0].keys():
            metric_total = 0

            for output in outputs:
                metric_value = output[metric_name]

                if self.trainer.use_dp or self.trainer.use_ddp2:
                    if metric_name in ["tp", "fp", "fn"]:
                        metric_value = torch.sum(metric_value)
                    else:
                        metric_value = torch.mean(metric_value)

                metric_total += metric_value
            if metric_name in ["tp", "fp", "fn"]:
                tqdm_dict[metric_name] = metric_total
            else:
                tqdm_dict[metric_name] = metric_total / len(outputs)

        prec_rec_f1 = f1_score(tqdm_dict["tp"], tqdm_dict["fp"], tqdm_dict["fn"])
        tqdm_dict.update(prec_rec_f1)
        result = {
            "progress_bar": tqdm_dict,
            "log": tqdm_dict,
            "val_loss": tqdm_dict["val_loss"],
        }
        return result

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_end(self, outputs):
        return self.validation_end(outputs)

    def configure_optimizers(self):
        """
        returns the optimizer and scheduler
        """
        opt_parameters = self.sentence_embeds_model.layerwise_lr(
            self.hparams.lr, self.hparams.layerwise_decay
        )
        opt_parameters += [{"params": self.context_classifier_model.parameters()}]

        optimizer = torch.optim.AdamW(opt_parameters, lr=self.hparams.lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=10)
        return [optimizer], [scheduler]

    @pl.data_loader
    def train_dataloader(self):
        return dataloader(
            self.hparams.train_file,
            self.hparams.max_seq_len,
            self.hparams.bs,
            self.emo_dict,
            use_ddp=self.use_ddp,
        )

    @pl.data_loader
    def val_dataloader(self):
        return dataloader(
            self.hparams.val_file,
            self.hparams.max_seq_len,
            self.hparams.bs,
            self.emo_dict,
            use_ddp=self.use_ddp,
        )

    @pl.data_loader
    def test_dataloader(self):
        return dataloader(
            self.hparams.test_file,
            self.hparams.max_seq_len,
            self.hparams.bs,
            self.emo_dict,
            use_ddp=self.use_ddp,
        )

    @staticmethod
    def add_model_specific_args(parent_parser, root_dir):
        """
        parameters defined here will be available to the model through self.hparams
        """
        # fmt: off
        parser = HyperOptArgumentParser(parents=[parent_parser])

        parser.opt_list('--bs', default=64, type=int, options=[32, 128, 256], tunable=True,
                        help='mini-batch size (default: 256), this is the total batch size of all GPUs'
                        'on the current node when using Data Parallel or Distributed Data Parallel')
        parser.opt_list('--projection_size', default=256, type=int, options=[32, 128, 512], tunable=True,
                       help='sentence embedding size and hidden size for the second transformer')
        parser.opt_list('--n_layers', default=1, type=int, options=[2, 4, 6], tunable=True,
                       help='number of encoder layers for the second transformer')
        parser.opt_list('--frozen_epochs', default=2, type=int, options=[3, 6, 9], tunable=True,
                       help='number of epochs the pretrained DistilBert is frozen')
        parser.opt_range('--lr', default=2.0e-5, type=float, tunable=True, low=1.0e-5, high=5.0e-4,
                         nb_samples=5, help='initial learning rate')
        parser.opt_list('--layerwise_decay', default=0.95, type=float, options=[0.3, 0.6, 0.8], tunable=True,
                       help='layerwise decay factor for the learning rate of the pretrained DistilBert')
        parser.opt_list('--max_seq_len', default=32, type=int, options=[16, 64], tunable=False,
                       help='maximal number of input tokens for the DistilBert model')
        parser.opt_list('--dropout', default=0.1, type=float, options=[0.1, 0.2], tunable=False)
        parser.add_argument('--train_file', default=os.path.join(root_dir, 'data/clean_train.txt'), type=str)
        parser.add_argument('--val_file', default=os.path.join(root_dir, 'data/clean_val.txt'), type=str)
        parser.add_argument('--test_file', default=os.path.join(root_dir, 'data/clean_test.txt'), type=str)
        parser.add_argument('--epochs', default=3, type=int, metavar='N',
                            help='number of total epochs to run')
        parser.add_argument('--seed', type=int, default=None,
                            help='seed for initializing training')
        # fmt: on

        return parser


# Cell
def get_args(model):
    """
    returns the HyperOptArgumentParser
    """
    # fmt: off
    parent_parser = HyperOptArgumentParser(strategy='random_search', add_help = False)

    data_dir = os.getcwd()
    parent_parser.add_argument('--mode', type=str, default='test',
                               choices=('default', 'test', 'hparams_search'),
                               help='supports default for train/test/val and hparams_search for a hyperparameter search')
    parent_parser.add_argument('--save-path', metavar='DIR', default=os.environ['HOME'] + "/data/mlflow_experiments/mlruns", type=str,
                               help='path to save output')
    parent_parser.add_argument('--gpus', type=str, default='0,1', help='which gpus')
    parent_parser.add_argument('--distributed-backend', type=str, default='ddp', choices=('dp', 'ddp', 'ddp2'),
                               help='supports three options dp, ddp, ddp2')
    parent_parser.add_argument('--use_16bit', dest='use_16bit', action='store_true',
                               help='if true uses 16 bit precision')

    # debugging
    parent_parser.add_argument('--fast_dev_run', dest='fast_dev_run', action='store_true',
                               help='debugging a full train/val/test loop')
    parent_parser.add_argument('--track_grad_norm', dest='track_grad_norm', action='store_true',
                               help='inspect gradient norms')

    parser = model.add_model_specific_args(parent_parser, data_dir)
    # fmt: on
    return parser


def setup_mlflowlogger_and_checkpointer(hparams):
    mlflow_logger = MLFlowLogger(
        experiment_name="exp_name", tracking_uri=hparams.save_path
    )
    run_id = mlflow_logger.run_id
    checkpoints_folder = os.path.join(
        hparams.save_path, mlflow_logger._expt_id, run_id, "checkpoints"
    )
    os.makedirs(checkpoints_folder, exist_ok=True)
    checkpoint = ModelCheckpoint(
        filepath=checkpoints_folder, monitor="val_loss", save_top_k=1
    )
    return checkpoint, mlflow_logger, run_id


def main(hparams, gpus=None):
    model = EmotionModel(hparams)

    if hparams.seed is not None:
        random.seed(hparams.seed)
        torch.manual_seed(hparams.seed)
        torch.backends.cudnn.deterministic = True

    early_stop_callback = pl.callbacks.EarlyStopping(
        monitor="val_loss", min_delta=0.00, patience=5, verbose=False, mode="min"
    )

    checkpoint, mlflow_logger, run_id = setup_mlflowlogger_and_checkpointer(hparams)

    trainer = pl.Trainer(
        logger=mlflow_logger,
        checkpoint_callback=checkpoint,
        default_save_path=hparams.save_path,
        gpus=len(gpus.split(",")) if gpus else hparams.gpus,
        distributed_backend=hparams.distributed_backend,
        use_amp=hparams.use_16bit,
        early_stop_callback=early_stop_callback,
        max_nb_epochs=hparams.epochs,
        log_gpu_memory="all",
        fast_dev_run=hparams.fast_dev_run,
        track_grad_norm=(2 if hparams.track_grad_norm else -1),
    )
    trainer.fit(model)
    mlflow_logger.experiment.log_artifacts(run_id, checkpoint.dirpath)

    if hparams.mode == "test":
        trainer.test()
