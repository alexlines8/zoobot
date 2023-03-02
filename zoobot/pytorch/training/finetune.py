# Based on Inigo's BYOL FT step
# https://github.com/inigoval/finetune/blob/main/finetune.py
import logging
import os
import warnings
from functools import partial

import pytorch_lightning as pl
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint

import torch
import torch.nn.functional as F
import torchmetrics as tm

from zoobot.pytorch.training import losses
from zoobot.pytorch.estimators import define_model

# https://discuss.pytorch.org/t/how-to-freeze-bn-layers-while-training-the-rest-of-network-mean-and-var-wont-freeze/89736/7
# I do this recursively and only for BatchNorm2d (not dropout, which I still want active)


def freeze_batchnorm_layers(model):
    for name, child in (model.named_children()):
        if isinstance(child, torch.nn.BatchNorm2d):
            logging.debug('freezing {} {}'.format(child, name))
            child.eval()  # no grads, no param updates, no statistic updates
        else:
            freeze_batchnorm_layers(child)  # recurse


class FinetuneableZoobotAbstract(pl.LightningModule):

    def __init__(
        self,
        # can provide either checkpoint_loc, and will load this model as encoder...
        checkpoint_loc=None,
        # ...or directly pass model to use as encoder
        encoder=None,
        encoder_dim=1280,  # as per current Zooot. TODO Could get automatically?
        n_epochs=100,  # TODO early stopping
        n_layers=0,  # how many layers deep to FT
        batch_size=1024,
        lr_decay=0.75,
        weight_decay=0.05,
        learning_rate=1e-4,
        dropout_prob=0.5,
        freeze_batchnorm=True,
        prog_bar=True,
        visualize_images=False,  # upload examples to wandb, good for debugging
        seed=42
    ):
        super().__init__()

        # adds every __init__ arg to model.hparams
        # will also add to wandb if using logging=wandb, I think
        # necessary if you want to reload!
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # this raises a warning that encoder is already a Module hence saved in checkpoint hence no need to save as hparam
            # true - except we need it to instantiate this class, so it's really handy to have saved as well
            # therefore ignore the warning
            self.save_hyperparameters()

        if checkpoint_loc is not None:
          assert encoder is None, 'Cannot pass both checkpoint to load and encoder to use'
          self.encoder = load_pretrained_encoder(checkpoint_loc)
        else:
          assert checkpoint_loc is None, 'Cannot pass both checkpoint to load and encoder to use'
          self.encoder = encoder

        self.encoder_dim = encoder_dim
        self.n_layers = n_layers
        self.freeze = True if n_layers == 0 else False

        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.lr_decay = lr_decay
        self.weight_decay = weight_decay
        self.dropout_prob = dropout_prob
        self.n_epochs = n_epochs

        self.freeze_batchnorm = freeze_batchnorm

        if self.freeze_batchnorm:
            freeze_batchnorm_layers(self.encoder)  # inplace

        self.seed = seed
        self.prog_bar = prog_bar
        self.visualize_images = visualize_images

    def configure_optimizers(self):

        if self.freeze:
            params = self.head.parameters()
            return torch.optim.AdamW(params, lr=self.learning_rate)
        else:
            lr = self.learning_rate
            params = [{"params": self.head.parameters(), "lr": lr}]

            # this bit is specific to Zoobot EffNet
            # zoobot model is single Sequential()
            effnet_with_pool = list(self.encoder.children())[0]

            layers = [layer for layer in effnet_with_pool.children(
            ) if isinstance(layer, torch.nn.Sequential)]
            layers.reverse()  # inplace. first element is now upper-most layer

            assert self.n_layers <= len(
                layers
            ), f"Network only has {len(layers)} layers, {self.n_layers} specified for finetuning"

            # Append parameters of layers for finetuning along with decayed learning rate
            for i, layer in enumerate(layers[: self.n_layers]):
                params.append({
                    "params": layer.parameters(),
                    "lr": lr * (self.lr_decay**i)
                })

            # Initialize AdamW optimizer
            opt = torch.optim.AdamW(
                params, weight_decay=self.weight_decay, betas=(0.9, 0.999))  # higher weight decay is typically good

            return opt


    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.encoder(x)
        x = self.head(x)
        return x

    
    def make_step(self, batch):
      # part of training/val/test for all subclasses
        x, y = batch
        y_pred = self.forward(x)
        loss = self.loss(y, y_pred)
        # {'loss': loss.mean(), 'predictions': y_pred, 'labels': y}
        return y, y_pred, loss


    # def on_train_batch_end(self, outputs, *args) -> None:
    #     self.log("finetuning/train_loss_batch",
    #              outputs['loss'], on_step=False, on_epoch=True, prog_bar=self.prog_bar)

    def train_epoch_end(self, outputs, *args) -> None:
        losses = torch.cat([batch_output['loss'].expand(0)
                           for batch_output in outputs])
        self.log("finetuning/train_loss",
                 losses.mean(), prog_bar=self.prog_bar)

        if hasattr(self, 'train_acc') is not None:
            self.log("finetuning/train_acc", self.train_acc,
                     prog_bar=self.prog_bar)  # log here

    def validation_epoch_end(self, outputs, *args) -> None:
        # calc. mean of losses over val batches as val loss
        losses = torch.FloatTensor([batch_output['loss']
                                   for batch_output in outputs])
        self.log("finetuning/val_loss", torch.mean(losses),
                 prog_bar=self.prog_bar)

        if hasattr(self, 'val_acc'):
            self.log("finetuning/val_acc", self.val_acc,
                     prog_bar=self.prog_bar)

    def on_test_batch_end(self, outputs, *args) -> None:
        self.log('test/test_loss', outputs['loss'])
        if hasattr(self, 'test_acc'):
            self.test_acc(outputs['predictions'], outputs['labels'])
            self.log(f"finetuning/test_acc", self.test_acc,
                     on_step=False, on_epoch=True)

    def on_validation_batch_end(self, outputs, batch, batch_idx, *args) -> None:
        # self.log(f"finetuning/val_loss_batch",
        #          outputs['loss'].mean(), on_step=False, on_epoch=True, prog_bar=self.prog_bar)
        
        if self.visualize_images:
          self.upload_images_to_wandb(outputs, batch, batch_idx)

    def upload_images_to_wandb(self, outputs, batch, batch_idx):
      raise NotImplementedError('Must be subclassed')



class FinetuneableZoobotClassifier(FinetuneableZoobotAbstract):

    def __init__(
            self,
            num_classes: int,
            label_smoothing=0.,
            **super_kwargs) -> None:

        super().__init__(**super_kwargs)

        logging.info('Using classification head and cross-entropy loss')
        self.head = LinearClassifier(
            input_dim=self.encoder_dim,
            output_dim=num_classes,
            dropout_prob=self.dropout_prob
        )
        self.label_smoothing = label_smoothing
        self.loss = partial(cross_entropy_loss,
                            label_smoothing=self.label_smoothing)
        self.train_acc = tm.Accuracy(task='binary', average="micro")
        self.val_acc = tm.Accuracy(task='binary', average="micro")
        self.test_acc = tm.Accuracy(task='binary', average="micro")


    def training_step(self, batch, batch_idx):
        y, y_pred, loss = self.make_step(batch)

        # calculate metrics
        y_class_preds = torch.argmax(y_pred, axis=1)
        self.train_acc(y_class_preds, y)  # update calc, but do not log

        return {'loss': loss.mean(), 'predictions': y_pred, 'labels': y}

    
    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        y, y_pred, loss = self.make_step(batch)

        y_class_preds = torch.argmax(y_pred, axis=1)
        self.val_acc(y_class_preds, y)

        return {'loss': loss.mean(), 'predictions': y_pred, 'labels': y}


    def upload_images_to_wandb(self, outputs, batch, batch_idx):
      # self.logger is set by pl.Trainer(logger=) argument
        if (self.logger is not None) and (batch_idx == 0):
            x, y = batch
            y_pred_softmax = F.softmax(outputs['predictions'], dim=1)[:, 1]  # odds of class 1 (assumed binary)
            n_images = 5
            images = [img for img in x[:n_images]]
            captions = [f'Ground Truth: {y_i} \nPrediction: {y_p_i}' for y_i, y_p_i in zip(
                y[:n_images], y_pred_softmax[:n_images])]
            self.logger.log_image(
                key='val_images',
                images=images,
                caption=captions)


class FinetuneableZoobotTree(FinetuneableZoobotAbstract):

    def __init__(
        self,
        schema=None,
        **super_kwargs

    ):
        super().__init__(**super_kwargs)

        logging.info('Using dropout+dirichlet head and dirichlet (count) loss')

        self.schema = schema
        self.output_dim = len(self.schema.label_cols)

        self.head = define_model.get_pytorch_dirichlet_head(
            encoder_dim=self.encoder_dim,
            output_dim=self.output_dim,
            test_time_dropout=False,
            dropout_rate=self.dropout_prob
        )
      
        self.loss = define_model.get_dirichlet_loss_func(self.schema.question_index_groups)

    def training_step(self, batch, batch_idx):
        y, y_pred, loss = self.make_step(batch)
        return {'loss': loss.mean(), 'predictions': y_pred, 'labels': y}

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
      # now identical to above
        y, y_pred, loss = self.make_step(batch)
        return {'loss': loss.mean(), 'predictions': y_pred, 'labels': y}

    def test_step(self, batch, batch_idx, dataloader_idx=0):
      # now identical to above
        y, y_pred, loss = self.make_step(batch)
        return {'loss': loss, 'predictions': y_pred, 'labels': y}

    def upload_images_to_wandb(self, outputs, batch, batch_idx):
      pass  # not yet implemented

# https://github.com/inigoval/byol/blob/1da1bba7dc5cabe2b47956f9d7c6277decd16cc7/byol_main/networks/models.py#L29
class LinearClassifier(torch.nn.Module):
    def __init__(self, input_dim, output_dim, dropout_prob=0.5):
        # input dim is representation dim, output_dim is num classes
        super(LinearClassifier, self).__init__()
        self.dropout = torch.nn.Dropout(p=dropout_prob)
        self.linear = torch.nn.Linear(input_dim, output_dim)

    def forward(self, x):
        # returns logits, as recommended for CrossEntropy loss
        x = self.dropout(x)
        x = self.linear(x)
        return x

    def predict_step(self, x):
        x = self.forward(x)  # logits
        # then applies softmax
        return F.softmax(x, dim=1)[:, 1]


def cross_entropy_loss(y, y_pred, label_smoothing=0.):
    # y should be shape (batch) and ints
    # y_pred should be shape (batch, classes)
    # note the flipped arg order (sklearn convention in my func)
    # returns loss of shape (batch)
    # will reduce myself
    return F.cross_entropy(y_pred, y.long(), label_smoothing=label_smoothing, reduction='none')


def dirichlet_loss(y, y_pred, question_index_groups):
    # aggregation equiv. to sum(axis=1).mean(), but fewer operations
    # returns loss of shape (batch)
    return losses.calculate_multiquestion_loss(y, y_pred, question_index_groups).mean()*len(question_index_groups)


class FinetunedZoobotLightningModuleBaseline(FinetuneableZoobotClassifier):
    # exactly as the Finetuned model above, but with a simple single learning rate
    # useful for training from-scratch model exactly as if it were finetuned, as a baseline

    def configure_optimizers(self):
        head_params = list(self.head.parameters())
        encoder_params = list(self.encoder.parameters())
        return torch.optim.AdamW(head_params + encoder_params, lr=self.learning_rate)


def load_pretrained_encoder(checkpoint_loc: str) -> torch.nn.Sequential:
    return define_model.ZoobotTree.load_from_checkpoint(
        checkpoint_loc).encoder

def get_trainer(
    save_dir,
    file_template="{epoch}",
    save_top_k=1,
    max_epochs=100,
    patience=10,
    devices=None,
    accelerator='auto',
    logger=None,
    **trainer_kwargs
):
    # custom_config, encoder, datamodule, save_dir, logger=None, baseline=False):
    # this is a convenient interface for users not wanting to configure everything in detail
    # advanced users can import the classes above directly

    checkpoint_callback = ModelCheckpoint(
        monitor='finetuning/val_loss',
        every_n_epochs=1,
        save_on_train_epoch_end=True,
        auto_insert_metric_name=False,
        verbose=True,
        dirpath=os.path.join(save_dir, 'checkpoints'),
        filename=file_template,
        save_weights_only=True,
        save_top_k=save_top_k
    )

    early_stopping_callback = EarlyStopping(
        monitor='finetuning/val_loss',
        mode='min',
        patience=patience
    )

    # Initialise pytorch lightning trainer
    trainer = pl.Trainer(
        logger=logger,
        callbacks=[checkpoint_callback, early_stopping_callback],
        max_epochs=max_epochs,
        accelerator=accelerator,
        devices=devices,
        **trainer_kwargs,
    )

    return trainer

    # when ready (don't peek often, you'll overfit)
    # trainer.test(model, dataloaders=datamodule)

    # return model, checkpoint_callback.best_model_path
    # trainer.callbacks[checkpoint_callback].best_model_path?

# def investigate_structure():

#     from zoobot.pytorch.estimators import define_model


#     model = define_model.get_plain_pytorch_zoobot_model(output_dim=1280, include_top=False)

#     # print(model)
#     # with include_top=False, first and only child is EffNet
#     effnet_with_pool = list(model.children())[0]

#     # 0th is actually EffNet, 1st and 2nd are AvgPool and Identity
#     effnet = list(effnet_with_pool.children())[0]

#     for layer_n, layer in enumerate(effnet.children()):
#         # first bunch are Sequential module wrapping e.g. 3 MBConv blocks
#         print('\n', layer_n)
#         if isinstance(layer, torch.nn.Sequential):
#             print(layer)
#     # so the blocks to finetune are each Sequential (repeated MBConv) block
#     # and other blocks can be left alone
#     # (also be careful to leave batch-norm alone)
