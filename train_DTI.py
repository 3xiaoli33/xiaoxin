import copy
from time import time
import os
#os.environ["CUDA_VISIBLE_DEVICES"] = '0'

import sys
import numpy as np
import pandas as pd

import torch
import json
from torch import nn
from torch.autograd import Variable
from torch.utils import data
from tqdm import tqdm
import typing as T
import torchmetrics
from argparse import ArgumentParser

#import wandb
from omegaconf import OmegaConf
from pathlib import Path

from src import architectures as model_types
from src.data import (
    get_task_dir,
    DTIDataModule,
    TDCDataModule,
    DUDEDataModule,
    EnzPredDataModule,
    CSVDataModule,
    TestModule
)
from src.utils import (
    set_random_seed,
    config_logger,
    get_logger,
    get_featurizer,
    sigmoid_cosine_distance_p,
)

from src.margin import MarginScheduledLossFunction

print(f"GPU count ={torch.cuda.device_count()}")

logg = get_logger()

parser = ArgumentParser(description="PLM_DTI Training.")
parser.add_argument(
    "--exp-id", help="Experiment ID", dest="experiment_id", default='yongbo_dti_dg',
)
parser.add_argument(
    "--config", help="YAML config file", default="configs/multiclass_config.yaml"
)

parser.add_argument(
    "--wandb-proj",
    help="Weights and Biases Project",
    dest="wandb_proj",
    default="wandb_proj",
)
parser.add_argument(
    "--task",
    choices=[
        "biosnap",
        "bindingdb",
        "davis",
        "biosnap_prot",
        "biosnap_mol",
        "dti_dg",
        "bindingdb_v2",
        "bindingdb_multi_class",
        "bindingdb_multi_class_small"
    ],
    type=str,
    help="Task name. Could be biosnap, bindingdb, davis, biosnap_prot, biosnap_mol.",
)

parser.add_argument(
    "--drug-featurizer", help="Drug featurizer", dest="drug_featurizer"
)
parser.add_argument(
    "--target-featurizer", help="Target featurizer", dest="target_featurizer"
)
parser.add_argument(
    "--distance-metric",
    help="Distance in embedding space to supervise with",
    dest="distance_metric",
    default="Cosine"
)
parser.add_argument("--epochs", type=int, help="number of total epochs to run")
parser.add_argument("-b", "--batch-size", type=int, help="batch size")
parser.add_argument(
    "--lr",
    "--learning-rate",
    type=float,
    help="initial learning rate",
    dest="lr",
    default= 1e-4
)
parser.add_argument(
    "--clr", type=float, help="initial learning rate", dest="clr",default=1e-5
)
parser.add_argument(
    "--r", "--replicate", type=int, help="Replicate", dest="replicate",default=0
)
parser.add_argument(
    "--d", "--device", type=int, help="CUDA device", dest="device",default=0
)
parser.add_argument(
    "--verbosity", type=int, help="Level at which to log", dest="verbosity",default=3
)
parser.add_argument(
    "--checkpoint", default=None, help="Model weights to start from"
)


def test(model, data_generator, metrics, device=None, classify=True):

    if device is None:
        logg.warning("device is cpu!")
        device = torch.device("cpu")

    for k, metric in metrics.items():
        metric = metric.to(device)
        metric.reset()
        metrics[k] = metric
    model.eval()

    data_len = len(data_generator)

    logg.info(f"start to compute metric , total samples : {data_len}")

    for i, batch in tqdm(enumerate(data_generator), total=len(data_generator)):

        pred, label = step(model, batch, device, is_train=False)
        if classify:
            label = label.int()
        else:
            label = label.float()

        for _, metric in metrics.items():
            metric(pred, label)

    results = {}
    for (k, metric) in metrics.items():
        res = metric.compute()
        results[k] = res

    for metric in metrics.values():
        metric.to("cpu")

    return results


def step(model, batch, device=None, is_train=True):

    if device is None:
        device = torch.device("cpu")

    drug, target, label = batch  # target is (D + N_pool)

    if isinstance(drug,dict):

        for k,v in drug.items():
            drug[k] = v.to(device)
    else:
        drug = drug.to(device)

    target = target.to(device)
    

    if isinstance(drug,dict):
        
        pred = model(drug['drug_input_ids'], drug['drug_att_masks'], target, is_train=is_train)
    else:
        pred = model(drug, target,is_train=is_train)

    label = Variable(torch.from_numpy(np.array(label)).float()).to(device)
    return pred, label


def contrastive_step(model, batch, device=None):

    if device is None:
        device = torch.device("cpu")

    anchor, positive, negative = batch

    anchor_projection = model.target_projector(anchor.to(device))
    positive_projection = model.drug_projector(positive.to(device))
    negative_projection = model.drug_projector(negative.to(device))

    return anchor_projection, positive_projection, negative_projection


# def wandb_log(m, do_wandb=True):
#     if do_wandb:
#         wandb.log(m)


def ordinal_regression_loss(y_pred, y_target):

    num_thresholds = y_pred.size(1)
    y_true_expanded = y_target.unsqueeze(1).repeat(1, num_thresholds)
    mask = (torch.arange(num_thresholds).to(y_pred.device).unsqueeze(0) < y_true_expanded).float()
    loss = torch.nn.BCELoss()(y_pred,mask)
    return loss


def main():
    # Get configuration


    args = parser.parse_args()
    config = OmegaConf.load(args.config)
    arg_overrides = {k: v for k, v in vars(args).items() if v is not None}
    config.update(arg_overrides)

    save_dir = f'{config.get("model_save_dir", ".")}/{config.experiment_id}'
    os.makedirs(save_dir, exist_ok=True)

    # Logging
    if "log_file" not in config:
        config.log_file = None
    else:
        os.makedirs(Path(config.log_file).parent, exist_ok=True)
    config_logger(
        config.log_file,
        "%(asctime)s [%(levelname)s] %(message)s",
        config.verbosity,
        use_stdout=True,
    )

    # Set CUDA device
    device_no = config.device
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")
    logg.info(f"Using CUDA device {device}")

    # Set random state
    logg.debug(f"Setting random state {config.replicate}")
    set_random_seed(config.replicate)

    # Load DataModule
    logg.info("Preparing DataModule")
    task_dir = get_task_dir(config.task)

    drug_featurizer = get_featurizer(config.drug_featurizer, save_dir=task_dir)

    if config.model_architecture in ('DrugProteinAttention','DrugProteinMLP','ChemBertaProteinAttention','ChemBertaProteinAttention_Local'):
        per_tok=True
    else:
        per_tok=False

    logg.info(f"per_tok={per_tok}")
    target_featurizer = get_featurizer(
        config.target_featurizer, per_tok=per_tok, save_dir=task_dir
    )

    if config.model_architecture == "ChemBertaProteinAttention":

        config.classify = False
        config.watch_metric = "val/pcc"
        datamodule = CSVDataModule(
            task_dir, 
            drug_featurizer,
            target_featurizer,
            device=device,
            seed=config.replicate,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
            label_column=config.label_column
            )

    elif config.model_architecture == "ChemBertaProteinAttention_Local":

        config.classify = False
        config.watch_metric = "val/pcc"

        datamodule = TestModule(
            task_dir,
            drug_featurizer,
            target_featurizer,
            device=device,
            seed=config.replicate,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
            label_column=config.label_column
            )


    elif config.task in ("bindingdb_v2","dti_dg") :
        config.classify = False
        config.watch_metric = "val/pcc"
        datamodule = TDCDataModule(
            task_dir,
            drug_featurizer,
            target_featurizer,
            device=device,
            seed=config.replicate,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
            label_column=config.label_column
        )

    elif config.task in ("bindingdb_multi_class","bindingdb_multi_class_small") :

        config.classify = True
        config.watch_metric = "val/aupr"
        datamodule = TDCDataModule(
            task_dir,
            drug_featurizer,
            target_featurizer,
            device=device,
            seed=config.replicate,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
            label_column=config.label_column
        )

    elif config.task in EnzPredDataModule.dataset_list():
        config.classify = True
        config.watch_metric = "val/aupr"
        datamodule = EnzPredDataModule(
            task_dir,
            drug_featurizer,
            target_featurizer,
            device=device,
            seed=config.replicate,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
        )
    else:
        config.classify = True
        config.watch_metric = "val/aupr"
        datamodule = DTIDataModule(
            task_dir,
            drug_featurizer,
            target_featurizer,
            device=device,
            batch_size=config.batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
        )
    datamodule.prepare_data()
    datamodule.setup()

    # Load DataLoaders
    logg.info("Getting DataLoaders")
    training_generator = datamodule.train_dataloader()
    validation_generator = datamodule.val_dataloader()
    testing_generator = datamodule.test_dataloader()


    config.drug_shape = drug_featurizer.shape
    config.target_shape = target_featurizer.shape

    # Model
    logg.info("Initializing model")

    if config.task in ('bindingdb_multi_class',"bindingdb_multi_class_small"):

        model = getattr(model_types, config.model_architecture)(
            config.drug_shape,
            config.target_shape,
            latent_dimension=config.latent_dimension,
            latent_distance=config.latent_distance,
            classify=config.classify,
            num_classes=config.num_classes,
            loss_type=config.loss_type,
        )
    else:

        model = getattr(model_types, config.model_architecture)(
            config.drug_shape,
            config.target_shape,
            latent_dimension=config.latent_dimension,
            latent_distance=config.latent_distance,
            classify=config.classify,
        )
    
    model = model.to(device)
    # 使用 DataParallel 进行多GPU加速
    if torch.cuda.device_count() > 1:
        logg.info(f"Using {torch.cuda.device_count()} GPUs for DataParallel")
        model = nn.DataParallel(model)

    if "checkpoint" in config:
        state_dict = torch.load(config.checkpoint)
        model.load_state_dict(state_dict)

    logg.info(model)


    if config.contrastive:
        logg.info("Loading contrastive data (DUDE)")
        dude_drug_featurizer = get_featurizer(
            config.drug_featurizer, save_dir=get_task_dir("DUDe")
        )

        dude_target_featurizer = get_featurizer(
            config.target_featurizer, save_dir=get_task_dir("DUDe")
        )

        contrastive_datamodule = DUDEDataModule(
            config.contrastive_split,
            dude_drug_featurizer,
            dude_target_featurizer,
            device=device,
            batch_size=config.contrastive_batch_size,
            shuffle=config.shuffle,
            num_workers=config.num_workers,
        )

        contrastive_datamodule.prepare_data()
        contrastive_datamodule.setup(stage="fit")
        contrastive_generator = contrastive_datamodule.train_dataloader()
    

    # Optimizers
    logg.info("Initializing optimizers")
    opt = torch.optim.AdamW(model.parameters(), lr=config.lr)
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        opt, T_0=config.lr_t0
    )

    if config.contrastive:
        contrastive_loss_fct = MarginScheduledLossFunction(
            M_0=config.margin_max,
            N_epoch=config.epochs,
            N_restart=config.margin_t0,
            update_fn=config.margin_fn,
        )
        opt_contrastive = torch.optim.AdamW(model.parameters(), lr=config.clr)
        lr_scheduler_contrastive = (
            torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
                opt_contrastive, T_0=config.clr_t0
            )
        )

    # Metrics
    logg.info("Initializing metrics")
    max_metric = 0
    model_max = copy.deepcopy(model)

    if config.task in("dti_dg","bindingdb_v2"):
        loss_fct = torch.nn.MSELoss()
        val_metrics = {
            "val/mse": torchmetrics.MeanSquaredError(),
            "val/pcc": torchmetrics.PearsonCorrCoef(),
        }

        test_metrics = {
            "test/mse": torchmetrics.MeanSquaredError(),
            "test/pcc": torchmetrics.PearsonCorrCoef(),
        }
    elif  config.task in ("bindingdb_multi_class","bindingdb_multi_class_small"):

        if config.loss_type == "OR":
            loss_fct = ordinal_regression_loss
        else:
            loss_fct = torch.nn.CrossEntropyLoss()
        val_metrics = {
            "val/class_accuracy": torchmetrics.classification.MulticlassAccuracy(num_classes=7,average=None),
            "val/class_recall": torchmetrics.classification.MulticlassRecall(num_classes=7, average=None),
            "val/aupr":torchmetrics.classification.MulticlassAveragePrecision(num_classes=7),
            "val/ConfusionMatrix":torchmetrics.ConfusionMatrix(task="multiclass", num_classes=7)
        }

        test_metrics = {
            "test/class_accuracy": torchmetrics.classification.MulticlassAccuracy(num_classes=7,average=None),
            "test/class_recall": torchmetrics.classification.MulticlassRecall(num_classes=7, average=None),
            "test/aupr":torchmetrics.classification.MulticlassAveragePrecision(num_classes=7),
            "test/ConfusionMatrix":torchmetrics.ConfusionMatrix(task="multiclass", num_classes=7)
        }
        

    else:
        loss_fct = torch.nn.BCELoss()
        val_metrics = {
            "val/aupr": torchmetrics.AveragePrecision(task="binary"),
            "val/auroc": torchmetrics.AUROC(task="binary"),
        }

        test_metrics = {
            "test/aupr": torchmetrics.AveragePrecision(task="binary"),
            "test/auroc": torchmetrics.AUROC(task="binary"),
        }

    # Initialize wandb
    """
    do_wandb = config.wandb_save and ("wandb_proj" in config)
    if do_wandb:
        logg.info(f"Initializing wandb project {config.wandb_proj}")
        wandb.init(
            project=config.wandb_proj,
            name=config.experiment_id,
            config=dict(config),
        )
        wandb.watch(model, log_freq=100)
    """
    
    logg.info("Config:")
    logg.info(json.dumps(dict(config), indent=4))

    logg.info("Beginning Training")

    #torch.backends.cudnn.benchmark = True

    # Begin Training
    start_time = time()
    for epo in range(config.epochs):
        model.train()
        epoch_time_start = time()

        # Main Step
        for i, batch in tqdm(
            enumerate(training_generator), total=len(training_generator)
        ):
            pred, label = step(
                model, batch, device
            )  # batch is (2048, 1024, 1)

            if config.classify:
                label = label.to(torch.int64)

            loss = loss_fct(pred, label)
            # logg.info(f"[Step Log] Epoch: {epo}, Step: {i + 1}, Loss: {loss.cpu().detach().numpy():8f}")
            opt.zero_grad()
            loss.backward()
            opt.step()

        """
              wandb_log(
                {
                    "train/step": (
                        epo * len(training_generator) * config.batch_size
                    )
                    + (i * config.batch_size),
                    "train/loss": loss,
                },
                do_wandb,
            )
            """

        lr_scheduler.step()
        """
           wandb_log(
            {
                "epoch": epo,
                "train/lr": lr_scheduler.get_lr()[0],
            },
            do_wandb,
        )
        
        """
     
        logg.info(
            f"Training at Epoch {epo + 1} with loss {loss.cpu().detach().numpy():8f}"
        )
        logg.info(f"Updating learning rate to {lr_scheduler.get_lr()[0]:8f}")

        # Contrastive Step
        if config.contrastive:
            logg.info(f"Training contrastive at Epoch {epo + 1}")
            for i, batch in tqdm(
                enumerate(contrastive_generator),
                total=len(contrastive_generator),
            ):

                anchor, positive, negative = contrastive_step(
                    model, batch, device
                )

                contrastive_loss = contrastive_loss_fct(
                    anchor, positive, negative
                )

                """
                 wandb_log(
                    {
                        "train/c_step": (
                            epo
                            * len(training_generator)
                            * config.contrastive_batch_size
                        )
                        + (i * config.contrastive_batch_size),
                        "train/c_loss": contrastive_loss,
                    },
                    do_wandb,
                )
                """

               

                opt_contrastive.zero_grad()
                contrastive_loss.backward()
                opt_contrastive.step()

            contrastive_loss_fct.step()
            lr_scheduler_contrastive.step()

            """
             wandb_log(
                {
                    "epoch": epo,
                    "train/triplet_margin": contrastive_loss_fct.margin,
                    "train/contrastive_lr": lr_scheduler_contrastive.get_lr(),
                },
                do_wandb,
            )
            
            """

            logg.info(
                f"Training at Contrastive Epoch {epo + 1} with loss {contrastive_loss.cpu().detach().numpy():8f}"
            )
            logg.info(
                f"Updating contrastive learning rate to {lr_scheduler_contrastive.get_lr()[0]:8f}"
            )
            logg.info(
                f"Updating contrastive margin to {contrastive_loss_fct.margin}"
            )

        # Validation
        if epo % config.every_n_val == 0:
            with torch.set_grad_enabled(False):

                val_results = test(
                    model,
                    validation_generator,
                    val_metrics,
                    device,
                    config.classify,
                )

                val_results["epoch"] = epo
                val_results["Charts/epoch_time"] = (
                    time() - epoch_time_start
                ) / config.every_n_val

                #wandb_log(val_results, do_wandb)

                if val_results[config.watch_metric] > max_metric:
                    logg.debug(
                        f"Validation AUPR {val_results[config.watch_metric]:8f} > previous max {max_metric:8f}"
                    )
                    model_max = copy.deepcopy(model)
                    max_metric = val_results[config.watch_metric]
                    model_save_path = Path(
                        f"{save_dir}/{config.experiment_id}_best_model_epoch{epo:02}.pt"
                    )
                    torch.save(
                        model_max.state_dict(),
                        model_save_path,
                    )
                    logg.info(f"Saving checkpoint model to {model_save_path}")

                    """

                     if do_wandb:
                        art = wandb.Artifact(
                            f"dti-{config.experiment_id}", type="model"
                        )
                        art.add_file(model_save_path, model_save_path.name)
                        wandb.log_artifact(art, aliases=["best"])


                    """

                logg.info(f"Validation at Epoch {epo + 1}")
                for k, v in val_results.items():
                    if not k.startswith("_"):
                        logg.info(f"{k}: {v}")
        

        # Testing
        logg.info("Beginning testing")
        try:
            with torch.set_grad_enabled(False):
                model_max = model_max.eval()

                test_start_time = time()
                test_results = test(
                    model_max,
                    testing_generator,
                    test_metrics,
                    device,
                    config.classify,
                )
                test_end_time = time()

                test_results["epoch"] = epo + 1
                test_results["test/eval_time"] = test_end_time - test_start_time
                test_results["Charts/wall_clock_time"] = 0
                #wandb_log(test_results, do_wandb)

                logg.info("epoch Testing")
                for k, v in test_results.items():
                    if not k.startswith("_"):
                        logg.info(f"{k}: {v}")

                #model_save_path = Path(
                    #f"{save_dir}/{config.experiment_id}_best_model.pt"
                #)
                #torch.save(
                   # model_max.state_dict(),
                    #model_save_path,
                #)
                #logg.info(f"Saving final model to {model_save_path}")
                """

                if do_wandb:
                    art = wandb.Artifact(
                        f"dti-{config.experiment_id}", type="model"
                    )
                    art.add_file(model_save_path, model_save_path.name)
                    wandb.log_artifact(art, aliases=["best"])


                """


        except Exception as e:
            logg.error(f"Testing failed with exception {e}")

    end_time = time()

    # Testing
    logg.info("Beginning testing")
    try:
        with torch.set_grad_enabled(False):
            model_max = model_max.eval()

            test_start_time = time()
            test_results = test(
                model_max,
                testing_generator,
                test_metrics,
                device,
                config.classify,
            )
            test_end_time = time()

            test_results["epoch"] = epo + 1
            test_results["test/eval_time"] = test_end_time - test_start_time
            test_results["Charts/wall_clock_time"] = end_time - start_time
            #wandb_log(test_results, do_wandb)

            logg.info("Final Testing")
            for k, v in test_results.items():
                if not k.startswith("_"):
                    logg.info(f"{k}: {v}")

            model_save_path = Path(
                f"{save_dir}/{config.experiment_id}_best_model.pt"
            )
            torch.save(
                model_max.state_dict(),
                model_save_path,
            )
            logg.info(f"Saving final model to {model_save_path}")
            """

              if do_wandb:
                art = wandb.Artifact(
                    f"dti-{config.experiment_id}", type="model"
                )
                art.add_file(model_save_path, model_save_path.name)
                wandb.log_artifact(art, aliases=["best"])

                
            """

    except Exception as e:
        logg.error(f"Testing failed with exception {e}")

    return model_max

if __name__ == "__main__":

    # torch.multiprocessing.set_start_method('spawn')
    best_model = main()
