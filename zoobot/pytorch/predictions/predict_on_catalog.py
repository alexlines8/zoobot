import logging
import time
import datetime
from typing import List

import pandas as pd
import numpy as np
import pytorch_lightning as pl

from zoobot.shared import save_predictions
from pytorch_galaxy_datasets.galaxy_datamodule import GalaxyDataModule


def predict(catalog: pd.DataFrame, model: pl.LightningModule, n_samples: int, label_cols: List, save_loc: str, datamodule_kwargs, trainer_kwargs):

    image_id_strs = list(catalog['id_str'])

    test_datamodule = GalaxyDataModule(
        label_cols=label_cols,
        # can take either a catalog (and split it), or a pre-split catalog
        test_catalog=catalog,  # no need to specify the other catalogs
        # will use the default transforms unless overridden with datamodule_kwargs
        # 
        **datamodule_kwargs  # e.g. batch_size, resize_size, crop_scale_bounds, etc.
    )
    # with this stage arg, will only use test catalog 
    # important to use test stage to avoid shuffling
    test_datamodule.setup(stage='test')  

    # set up trainer (again)
    trainer = pl.Trainer(
        **trainer_kwargs  # e.g. gpus
    )

    # from here, very similar to tensorflow version - could potentially refactor

    logging.info('Beginning predictions')
    start = datetime.datetime.fromtimestamp(time.time())
    logging.info('Starting at: {}'.format(start.strftime('%Y-%m-%d %H:%M:%S')))

    predictions = np.stack([trainer.predict(model, test_datamodule) for n in range(n_samples)], axis=-1)
    logging.info('Predictions complete - {}'.format(predictions.shape))

    if save_loc.endswith('.csv'):      # save as pandas df
        save_predictions.predictions_to_csv(predictions, image_id_strs, label_cols, save_loc)
    elif save_loc.endswith('.hdf5'):
        save_predictions.predictions_to_hdf5(predictions, image_id_strs, label_cols, save_loc)
    else:
        logging.warning('Save format of {} not recognised - assuming csv'.format(save_loc))
        save_predictions.predictions_to_csv(predictions, image_id_strs, label_cols, save_loc)

    logging.info(f'Predictions saved to {save_loc}')

    end = datetime.datetime.fromtimestamp(time.time())
    logging.info('Completed at: {}'.format(end.strftime('%Y-%m-%d %H:%M:%S')))
    logging.info('Time elapsed: {}'.format(end - start))
