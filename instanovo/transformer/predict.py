from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any

import numpy as np
import polars as pl
import torch
import yaml
from torch.utils.data import DataLoader
from tqdm import tqdm

from instanovo.inference.knapsack import Knapsack
from instanovo.inference.knapsack_beam_search import KnapsackBeamSearchDecoder
from instanovo.transformer.dataset import collate_batch
from instanovo.transformer.dataset import SpectrumDataset
from instanovo.transformer.model import InstaNovo
from instanovo.utils.metrics import Metrics

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# flake8: noqa: CR001
def get_preds(
    data_path: str,
    model_path: str,
    config: dict[str, Any],
    denovo: bool = False,
    output_path: str | None = None,
    knapsack_path: str | None = None,
    device: str = "cuda",
) -> None:
    """Get predictions from a trained model."""
    if denovo and output_path is None:
        raise Exception("Must specify an output path in denovo mode.")

    logging.info(f"Loading data from {data_path}")
    df = pl.read_ipc(data_path)
    df = df.sample(fraction=config["subset"], seed=0)
    logging.info(
        f"Data loaded, evaluating {config['subset']*100:.1f}%, {df.shape[0]} samples in total."
    )

    vocab = list(config["residues"].keys())
    config["vocab"] = vocab
    s2i = {v: i for i, v in enumerate(vocab)}
    i2s = {i: v for i, v in enumerate(vocab)}

    ds = SpectrumDataset(df, s2i, config["n_peaks"], return_str=True, annotated=not denovo)

    dl = DataLoader(
        ds,
        batch_size=config["predict_batch_size"],
        num_workers=config["n_workers"],
        shuffle=False,
        collate_fn=collate_batch,
    )

    logging.info(f"Loading model {model_path}")
    model = InstaNovo(
        i2s=i2s,
        residues=config["residues"],
        dim_model=config["dim_model"],
        n_head=config["n_head"],
        dim_feedforward=config["dim_feedforward"],
        n_layers=config["n_layers"],
        dropout=config["dropout"],
        max_length=config["max_length"],
        max_charge=config["max_charge"],
        use_depthcharge=config["use_depthcharge"],
        enc_type=config["enc_type"],
        dec_type=config["dec_type"],
        dec_precursor_sos=config["dec_precursor_sos"],
    )

    model_state = torch.load(model_path, map_location="cpu")
    # check if PTL checkpoint
    if "state_dict" in model_state:
        model_state = {k.replace("model.", ""): v for k, v in model_state["state_dict"].items()}

    model.load_state_dict(model_state)

    model = model.to(device)
    model = model.eval()

    # setup decoder
    if knapsack_path is None or not os.path.exists(knapsack_path):
        logging.info("Knapsack path missing or not specified, generating...")
        knapsack = _setup_knapsack(model)
        decoder = KnapsackBeamSearchDecoder(model, knapsack)
        if knapsack_path is not None:
            logging.info(f"Saving knapsack to {knapsack_path}")
            knapsack.save(knapsack_path)
    else:
        logging.info("Knapsack path found. Loading...")
        decoder = KnapsackBeamSearchDecoder.from_file(model=model, path=knapsack_path)

    index_cols = [
        "id",
        "global_index",
        "spectrum_index",
        "file_index",
        "sample",
        "file",
        "index",
        "fileno",
    ]
    cols = [x for x in df.columns if x in index_cols]

    pred_df = df.to_pandas()[cols].copy()

    preds = []
    targs = []
    probs = []

    start = time.time()
    for _, batch in tqdm(enumerate(dl), total=len(dl)):
        spectra, precursors, spectra_mask, peptides, _ = batch
        spectra = spectra.to(device)
        precursors = precursors.to(device)
        spectra_mask = spectra_mask.to(device)

        with torch.no_grad():
            p = decoder.decode(
                spectra=spectra,
                precursors=precursors,
                beam_size=config["n_beams"],
                max_length=config["max_length"],
            )

            preds += ["".join(x.sequence) if type(x) != list else "" for x in p]
            probs += [x.log_probability if type(x) != list else -1 for x in p]
            targs += list(peptides)

    delta = time.time() - start

    logging.info(f"Time taken for {data_path} is {delta:.1f} seconds")
    logging.info(
        f"Average time per batch (bs={config['predict_batch_size']}): {delta/len(dl):.1f} seconds"
    )

    if output_path is not None:
        if not denovo:
            pred_df["targets"] = targs
        pred_df["preds"] = preds
        pred_df["log_probs"] = probs

        pred_df.to_csv(output_path, index=False)
        logging.info(f"Predictions saved to {output_path}")

    # calculate metrics
    if not denovo:
        metrics = Metrics(config["residues"], config["isotope_error_range"])

        aa_prec, aa_recall, pep_recall, pep_prec = metrics.compute_precision_recall(
            pred_df["targets"], pred_df["preds"]
        )
        aa_er = metrics.compute_aa_er(pred_df["targets"], pred_df["preds"])
        auc = metrics.calc_auc(pred_df["targets"], pred_df["preds"], np.exp(pred_df["log_probs"]))

        logging.info(f"Performance on {data_path}:")
        logging.info(f"aa_er       {aa_er}")
        logging.info(f"aa_prec     {aa_prec}")
        logging.info(f"aa_recall   {aa_recall}")
        logging.info(f"pep_prec    {pep_prec}")
        logging.info(f"pep_recall  {pep_recall}")
        logging.info(f"auc         {auc}")


def main() -> None:
    """Predict with the model."""
    logging.info("Initializing inference.")

    parser = argparse.ArgumentParser()

    parser.add_argument("data_path")
    parser.add_argument("model_path")
    parser.add_argument("--denovo", action="store_true")
    parser.add_argument("--config", default="base.yaml")
    parser.add_argument("--output_path", default=None)
    parser.add_argument("--subset", default=1.0)
    parser.add_argument("--knapsack_path", default=None)
    parser.add_argument("--n_workers", default=8)

    args = parser.parse_args()

    config_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), f"../../configs/instanovo/{args.config}"
    )

    with open(config_path) as f_in:
        config = yaml.safe_load(f_in)

    config["n_workers"] = int(args.n_workers)
    config["subset"] = float(args.subset)
    data_path = args.data_path
    model_path = args.model_path
    denovo = args.denovo
    output_path = args.output_path
    knapsack_path = args.knapsack_path

    get_preds(data_path, model_path, config, denovo, output_path, knapsack_path)


def _setup_knapsack(model: InstaNovo) -> Knapsack:
    MASS_SCALE = 10000
    residue_masses = model.peptide_mass_calculator.masses
    residue_masses["$"] = 0
    residue_indices = model.decoder._aa2idx
    return Knapsack.construct_knapsack(
        residue_masses=residue_masses,
        residue_indices=residue_indices,
        max_mass=4000.00,
        mass_scale=MASS_SCALE,
    )


if __name__ == "__main__":
    main()
