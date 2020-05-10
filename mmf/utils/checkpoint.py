# Copyright (c) Facebook, Inc. and its affiliates.

import glob
import importlib
import os
import sys
import warnings

import torch
from omegaconf import OmegaConf

from mmf.common.registry import registry
from mmf.utils.configuration import get_mmf_env, load_yaml
from mmf.utils.distributed import is_master, synchronize
from mmf.utils.download import download_pretrained_model
from mmf.utils.file_io import PathManager
from mmf.utils.general import updir

try:
    import git
except ImportError:
    git = None


def _hack_imports():
    # NOTE: This can probably be made universal to support backwards
    # compatibility with name "pythia" if needed.
    sys.modules["pythia"] = importlib.import_module("mmf")
    sys.modules["pythia.utils.configuration"] = importlib.import_module(
        "mmf.utils.configuration"
    )


def load_pretrained_model(model_name_or_path, *args, **kwargs):
    # If this is a file, then load this directly else download and load
    if PathManager.exists(model_name_or_path):
        download_path = model_name_or_path
        model_name = model_name_or_path
    else:
        download_path = download_pretrained_model(model_name_or_path, *args, **kwargs)
        model_name = model_name_or_path

    configs = glob.glob(os.path.join(download_path, "*.yaml"))
    assert len(configs) <= 1, (
        "Multiple yaml files with the pretrained model. "
        + "MMF doesn't know what to do."
    )

    ckpts = []
    allowed_ckpt_types = ("*.ckpt", "*.pth", "*.pt")
    for ckpt_type in allowed_ckpt_types:
        ckpts.extend(glob.glob(os.path.join(download_path, ckpt_type)))

    assert (
        len(ckpts) == 1
    ), "None or multiple checkpoints files. MMF doesn't know what to do."

    _hack_imports()

    ckpt = torch.load(ckpts[0], map_location=lambda storage, loc: storage)
    # If configs are not present, will ckpt provide the config?
    if len(configs) == 0:
        assert "config" in ckpt, (
            "No configs provided with pretrained model "
            " while checkpoint also doesn't have configuration."
        )
        config = ckpt["config"]
    else:
        config = load_yaml(configs[0])

    model_config = config.get("model_config", config)
    ckpt = ckpt.get("model", ckpt)
    # Also handle the case of model_name is path
    model_config = model_config.get(model_name.split(os.path.sep)[-1].split(".")[0])

    return {"config": model_config, "checkpoint": ckpt, "full_config": config}


class Checkpoint:
    def __init__(self, trainer):
        """
        Generates a path for saving model which can also be used for resuming
        from a checkpoint.
        """
        self.trainer = trainer

        self.config = self.trainer.config
        self.save_dir = get_mmf_env(key="save_dir")
        self.model_name = self.config.model

        self.ckpt_foldername = self.save_dir

        self.device = registry.get("current_device")

        self.ckpt_prefix = ""

        if hasattr(self.trainer.model, "get_ckpt_name"):
            self.ckpt_prefix = self.trainer.model.get_ckpt_name() + "_"

        self.pth_filepath = os.path.join(
            self.ckpt_foldername, self.ckpt_prefix + self.model_name + "_final.pth"
        )

        self.models_foldername = os.path.join(self.ckpt_foldername, "models")
        if not PathManager.exists(self.models_foldername):
            PathManager.mkdirs(self.models_foldername)

        self.save_config()

        self.repo_path = updir(os.path.abspath(__file__), n=3)
        self.git_repo = self.config.checkpoint.save_git_details
        if git:
            self.git_repo = git.Repo(self.repo_path)

    def save_config(self):
        cfg_file = os.path.join(self.ckpt_foldername, "config.yaml")
        with PathManager.open(cfg_file, "w") as f:
            # Pop out config_override if present to remove clutter in
            # saved configuration yaml file
            self.config.pop("config_override", None)
            f.write(self.config.pretty(resolve=True))

    def load_state_dict(self):
        ckpt_config = self.config.checkpoint

        suffix = "best.ckpt" if ckpt_config.resume_best else "current.ckpt"
        reverse_suffix = "best.ckpt" if not ckpt_config.resume_best else "current.ckpt"
        ckpt_filepath = os.path.join(self.ckpt_foldername, self.ckpt_prefix + suffix)

        # In case of interrupts and resume, ckpt_config.resume_file would be there
        # But, if the checkpoints are already created in the save dir
        # and resume is true signifying the interrupt resume, we should skip
        # loading the resume file.
        if (
            ckpt_config.resume_file is not None or ckpt_config.resume_zoo is not None
        ) and (ckpt_config.resume is False or not PathManager.exists(ckpt_filepath)):
            if PathManager.exists(ckpt_config.resume_file):
                self._load(
                    ckpt_config.resume_file,
                    load_pretrained=ckpt_config.resume_pretrained,
                )
                return
            # resume_file doesn't exist, try from zoo now
            elif ckpt_config.resume_zoo is not None:
                self._load(
                    ckpt_config.resume_zoo,
                    load_pretrained=ckpt_config.resume_pretrained,
                )
            else:
                raise RuntimeError("{} doesn't exist".format(ckpt_config.resume_file))

        if ckpt_config.resume is True:
            if PathManager.exists(ckpt_filepath):
                self._load(ckpt_filepath)
            else:
                warnings.warn(
                    "Tried to resume but checkpoint filepath {} "
                    "is not present. Trying {}, otherwise skipping.".format(
                        ckpt_filepath, reverse_suffix
                    )
                )
                ckpt_filepath = ckpt_filepath.replace(suffix, reverse_suffix)
                if PathManager.exists(ckpt_filepath):
                    self._load(ckpt_filepath)

    def _load(self, file, force=False, load_pretrained=False):
        ckpt_config = self.config.checkpoint
        self.trainer.writer.write("Loading checkpoint")

        if self.training.config.resume_zoo:
            ckpt, should_continue = self._load_from_zoo()
            if not should_continue:
                return
        else:
            ckpt = self._torch_load(file)

        if "model" in ckpt:
            ckpt_model = ckpt["model"]
        else:
            ckpt_model = ckpt
            ckpt = {"model": ckpt}

        pretrained_state_mapping = ckpt_config.pretrained_state_mapping

        if load_pretrained is False or force is True:
            pretrained_state_mapping = {}

        new_dict = {}

        new_dict = self.upgrade_state_dict(ckpt_model)

        if len(pretrained_state_mapping.items()) == 0:
            final_dict = new_dict
            self.trainer.model.load_state_dict(final_dict, strict=False)

            reset_optimizer = ckpt_config.reset.optimizer or ckpt_config.reset.all
            if not reset_optimizer:
                self._load_optimizer(ckpt)

            self.trainer.early_stopping.init_from_checkpoint(ckpt)

            self.trainer.writer.write("Checkpoint loaded")

            reset_counts = ckpt_config.reset.all or ckpt_config.reset.counts

            if not reset_counts:
                self._load_counts()
        else:
            self._load_pretrained(new_dict)

    def _load_optimizer(self, ckpt):
        if "optimizer" in ckpt:
            try:
                self.trainer.optimizer.load_state_dict(ckpt["optimizer"])
            except ValueError:
                self.trainer.writer.write(
                    "Optimizer failed to load. Try with "
                    + "checkpoint.reset.optimizer=True"
                )
                raise
        else:
            warnings.warn(
                "'optimizer' key is not present in the "
                "checkpoint asked to be loaded. Skipping."
            )

    def _load_counts(self, ckpt):
        ckpt_config = self.trainer.config.checkpoint
        if "best_update" in ckpt:
            if ckpt_config.resume_best:
                self.trainer.num_updates = ckpt.get(
                    "best_update", self.trainer.num_updates
                )
                self.trainer.current_iteration = ckpt.get(
                    "best_iteration", self.trainer.current_iteration
                )
            else:
                self.trainer.num_updates = ckpt.get(
                    "num_updates", self.trainer.num_updates
                )
                self.trainer.current_iteration = ckpt.get(
                    "current_iteration", self.trainer.current_iteration
                )

            self.trainer.current_epoch = ckpt.get(
                "current_epoch", self.trainer.current_epoch
            )
        elif "best_iteration" in ckpt:
            # Preserve old behavior for old checkpoints where we always
            # load best iteration
            if ckpt_config.resume_best and "current_iteration" in ckpt:
                self.trainer.current_iteration = ckpt["current_iteration"]
            else:
                self.trainer.current_iteration = ckpt.get(
                    "best_iteration", self.trainer.current_iteration
                )

            self.trainer.num_updates = self.trainer.current_iteration

        registry.register("current_iteration", self.trainer.current_iteration)
        registry.register("num_updates", self.trainer.num_updates)

        self.trainer.current_epoch = ckpt.get("best_epoch", self.trainer.current_epoch)
        registry.register("current_epoch", self.trainer.current_epoch)

    def _load_pretrained(self, ckpt):
        model = self.trainer.model
        own_state = model.state_dict()
        mapping = self.trainer.config.checkpoint.pretrained_state_mapping
        for key, value in mapping.items():
            key += "."
            value += "."
            for attr in ckpt:
                for own_attr in own_state:
                    formatted_attr = model.format_state_key(attr)
                    if (
                        key in formatted_attr
                        and value in own_attr
                        and formatted_attr.replace(key, "")
                        == own_attr.replace(value, "")
                    ):
                        self.trainer.writer.write("Copying " + attr + " " + own_attr)
                        own_state[own_attr].copy_(ckpt[attr])
        self.trainer.writer.write("Pretrained model loaded")

    def upgrade_state_dict(self, state_dict):
        data_parallel = registry.get("data_parallel") or registry.get("distributed")
        new_dict = {}
        for attr in state_dict:
            new_attr = attr

            if data_parallel is False and attr.startswith("module."):
                # In case the ckpt was actually a data parallel model
                # replace first module. from dataparallel with empty string
                new_dict[new_attr.replace("module.", "", 1)] = state_dict[attr]
            elif data_parallel is not False and not attr.startswith("module."):
                new_dict["module." + new_attr] = state_dict[attr]
            else:
                new_dict[new_attr] = state_dict[attr]
        return new_dict

    def _load_from_zoo(self, file):
        ckpt_config = self.trainer.config.checkpoint
        zoo_ckpt = load_pretrained_model(file)

        # If zoo_override, load the model directly using `from_pretrained`
        if ckpt_config.zoo_override:
            model_cls = registry.get_model_class(self.trainer.config.model)
            self.trainer.model = model_cls.from_pretrained(ckpt_config.resume_zoo)
            self.trainer.config.model_config = zoo_ckpt["full_config"].model_config
            return None, False
        else:
            return zoo_ckpt["model"], True

    def _torch_load(self, file):
        # Backwards compatibility to Pythia
        _hack_imports()

        if "cuda" in str(self.device):
            return torch.load(file, map_location=self.device)
        else:
            return torch.load(file, map_location=lambda storage, loc: storage)

    def _get_vcs_fields(self):
        """Returns a dict with git fields of the current repository

           To reproduce an experiment directly from a checkpoint

           1) Export `config` key as a yaml
           2) Clone repository and checkout at given commit on given branch
           3) Any local change (diff) while running the experiment is stored
              in the value with key `git/diff`, output the diff to a `path.diff`
              file and apply the patch to the current state by simply

                           `patch -p0 < path.diff`
        """

        return {
            "git/branch": self.git_repo.active_branch.name,
            "git/commit_hash": self.git_repo.head.commit.name_rev,
            "git/commit_author": self.git_repo.head.commit.author.name,
            "git/commit_message": self.git_repo.head.commit.message,
            "git/diff": self.git_repo.git.diff("--no-prefix"),
        }

    def save(self, update, iteration, update_best=False):
        # Only save in main process
        if not is_master():
            return

        ckpt_filepath = os.path.join(self.models_foldername, "model_%d.ckpt" % update)
        best_ckpt_filepath = os.path.join(
            self.ckpt_foldername, self.ckpt_prefix + "best.ckpt"
        )
        current_ckpt_filepath = os.path.join(
            self.ckpt_foldername, self.ckpt_prefix + "current.ckpt"
        )

        best_iteration = self.trainer.early_stopping.best_monitored_iteration
        best_update = self.trainer.early_stopping.best_monitored_update
        best_metric = self.trainer.early_stopping.best_monitored_value
        model = self.trainer.model
        data_parallel = registry.get("data_parallel") or registry.get("distributed")

        if data_parallel is True:
            model = model.module

        ckpt = {
            "model": model.state_dict(),
            "optimizer": self.trainer.optimizer.state_dict(),
            "best_iteration": best_iteration,
            "current_iteration": registry.get("current_iteration"),
            "current_epoch": self.trainer.current_epoch,
            "num_updates": registry.get("num_updates"),
            "best_update": best_update,
            "best_metric_value": best_metric,
            # Convert to container to avoid any dependencies
            "config": OmegaConf.to_container(self.config, resolve=True),
        }

        if self.git_repo:
            git_metadata_dict = self._get_vcs_fields()
            ckpt.update(git_metadata_dict)

        torch.save(ckpt, ckpt_filepath)

        if update_best:
            torch.save(ckpt, best_ckpt_filepath)

        # Save current always
        torch.save(ckpt, current_ckpt_filepath)

    def restore(self):
        synchronize()
        self.trainer.writer.write("Restoring checkpoint")
        best_path = os.path.join(self.ckpt_foldername, self.ckpt_prefix + "best.ckpt")

        if PathManager.exists(best_path):
            self._load(best_path, force=True)

    def finalize(self):
        if is_master():
            torch.save(self.trainer.model.state_dict(), self.pth_filepath)
