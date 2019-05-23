#!/usr/bin/env python
#
# Author: Thamme Gowda [tg (at) isi (dot) edu]
# Created: 3/9/19

import argparse
from rtg import log, TranslationExperiment as Experiment
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rtg.module.decoder import Decoder
from rtg import RTG_PATH
from rtg.utils import IO, line_count
from dataclasses import dataclass
import torch
import random
from collections import defaultdict
from mosestokenizer import MosesDetokenizer
from sacrebleu import corpus_bleu, BLEU
import inspect
import copy
import json


@dataclass
class Pipeline:
    exp: Experiment
    script: Optional[Path] = None

    def pre_checks(self):
        # Some more validation needed
        assert self.exp.work_dir.exists()
        assert self.exp.config.get('prep') is not None
        assert self.exp.config.get('trainer') is not None
        assert self.exp.config.get('tester') is not None
        assert self.exp.config['tester']['suit'] is not None
        for name, (src, ref) in self.exp.config['tester']['suit'].items():
            src, ref = Path(src).resolve(), Path(ref).resolve()
            assert src.exists()
            assert ref.exists()
            assert line_count(src) == line_count(ref)

        script: Path = RTG_PATH / 'scripts' / 'detok-n-bleu.sh'
        if not script.exists():
            script = RTG_PATH.parent / 'scripts' / 'detok-n-bleu.sh'
        assert script.exists(), 'Unable to locate detok-n-bleu.sh script'
        self.script = script

    def detokenize(self, inp: Path, out: Path, col=0, lang='en', post_op=None):
        log.info(f"detok : {inp} --> {out}")
        tok_lines = IO.get_lines(inp, col=col, line_mapper=lambda x: x.split())
        with MosesDetokenizer(lang=lang) as detok:
            detok_lines = (detok(tok_line) for tok_line in tok_lines)
            if post_op:
                detok_lines = (post_op(line) for line in detok_lines)
            IO.write_lines(out, detok_lines)

    def evaluate_file(self, hyp: Path, ref: Path, lowercase=True) -> float:
        detok_hyp = hyp.with_name(hyp.name + '.detok')
        self.detokenize(hyp, detok_hyp)
        detok_lines = IO.get_lines(detok_hyp)
        ref_liness = [IO.get_lines(ref)]  # takes multiple refs, but here we have only one
        bleu: BLEU = corpus_bleu(sys_stream=detok_lines, ref_streams=ref_liness,
                                 lowercase=lowercase)
        bleu_str = f'BLEU = {bleu.score:.2f} {"/".join(f"{p:.1f}" for p in bleu.precisions)}' \
            f' (BP = {bleu.bp:.3f} ratio = {(bleu.sys_len / bleu.ref_len):.3f}' \
            f' hyp_len = {bleu.sys_len:d} ref_len={bleu.ref_len:d})'
        bleu_file = detok_hyp.with_suffix('.lc.bleu' if lowercase else '.oc.bleu')
        log.info(f'BLEU {hyp} : {bleu_str}')
        IO.write_lines(bleu_file, bleu_str)
        return bleu.score

    def decode_eval_file(self, decoder, src_file: Path, out_file: Path, ref_file: Path,
                         lowercase: bool, **dec_args) -> float:
        if out_file.exists() and out_file.stat().st_size > 0 \
                and line_count(out_file) == line_count(src_file):
            log.warning(f"{out_file} exists and has desired number of lines. Skipped...")
        else:
            log.info(f"decoding {src_file.name}")
            with IO.reader(src_file) as inp, IO.writer(out_file) as out:
                decoder.decode_file(inp, out, **dec_args)
        return self.evaluate_file(out_file, ref_file, lowercase=lowercase)

    def tune_decoder_params(self, exp: Experiment, tune_src: str, tune_ref: str,
                            trials: int = 20, strategy: str = 'random', lowercase=True,
                            beam_size=[1, 15], ensemble=[1, 10], lp_alpha=[0.0, 1.0],
                            suggested:List[Tuple[int, int, float]]=None,
                            **fixed_args):
        _, _, _, tune_args = inspect.getargvalues(inspect.currentframe())
        del tune_args['exp']  # exclude some args
        del tune_args['fixed_args']
        tune_args.update(fixed_args)

        _, step = exp.get_last_saved_model()
        tune_dir = exp.work_dir / f'tune_step{step}'
        log.info(f"Tune dir = {tune_dir}")
        tune_dir.mkdir(parents=True, exist_ok=True)
        assert strategy == 'random'  # only supported strategy for now
        tune_src, tune_ref = Path(tune_src), Path(tune_ref)
        assert tune_src.exists()
        assert tune_ref.exists()

        tune_log = tune_dir / 'scores.json'   # resume the tuning
        memory: Dict[Tuple, float] = {}
        if tune_log.exists():
            data = json.load(tune_log.open())
            # JSON keys cant be tuples, so they were stringified
            memory = {eval(k) : v for k, v in data.item()}

        beam_sizes, ensembles, lp_alphas = [], [], []
        if suggested:
            suggested = [(x[0], x[1], round(x[2], 2)) for x in suggested]
            suggested_new = [x for x in suggested if x not in memory]
            beam_sizes += [x[0] for x in suggested_new]
            ensembles += [x[1] for x in suggested_new]
            lp_alphas += [x[2] for x in suggested_new]

        new_trials = trials - len(memory)
        if new_trials > 0:
            beam_sizes += [random.randint(beam_size[0], beam_size[1]) for _ in range(new_trials)]
            ensembles += [random.randint(ensemble[0], ensemble[1]) for _ in range(new_trials)]
            lp_alphas += [round(random.uniform(lp_alpha[0], lp_alpha[1]), 2) for _ in range(new_trials)]

        # ensembling is somewhat costlier, so try minimize the model ensembling, by grouping them together
        grouped_ens = defaultdict(list)
        for b, ens, l in zip(beam_sizes, ensembles, lp_alphas):
            grouped_ens[ens].append((b, l))
        try:
            for ens, args in grouped_ens.items():
                decoder = Decoder.new(exp, ensemble=ens)
                for b_s, lp_a in args:
                    batch_size = self.suggest_batch_size(b_s)
                    name = f'tune_step{step}_beam{b_s}_ens{ens}_lp{lp_a:.2f}'
                    log.info(name)
                    out_file = tune_dir / f'{name}.out.tsv'
                    score = self.decode_eval_file(decoder, tune_src, out_file, tune_ref,
                                                  batch_size=batch_size, beam_size=b_s,
                                                  lp_alpha=lp_a, lowercase=lowercase, **fixed_args)
                    memory[(b_s, ens, lp_a)] =  score
            best_params = sorted(memory.items(), key=lambda x:x[0], reverse=True)[0]
            return dict(zip(['beam_size', 'ensemble', 'lp_alpha'], best_params)), tune_args
        finally:
            # JSON keys cant be tuples, so we stringify them
            data = {str(k): v for k, v in memory.items()}
            IO.write_lines(tune_log, json.dumps(data))

    def suggest_batch_size(self, beam_size):
        return 20000 // beam_size

    def run_tests(self, exp=None, args=None):
        if exp is None:
            exp = self.exp
        if not args:
            args = exp.config['tester']
        suit: Dict[str, List] = args['suit']
        assert suit
        log.info(f"Found {len(suit)} suit :: {suit.keys()}")

        _, step = exp.get_last_saved_model()
        if 'decoder' not in args:
            args['decoder'] = {}
        dec_args: Dict = args['decoder']
        best_params = copy.deepcopy(dec_args)
        max_len = best_params.get('max_len', 50)
        # TODO: this has grown to become messy (trying to make backward compatible, improve the logic here
        if 'tune' in dec_args and not dec_args['tune'].get('tuned'):
            tune_args: Dict = dec_args['tune']
            prep_args = exp.config['prep']
            if 'tune_src' not in tune_args:
                tune_args['tune_src'] = prep_args['valid_src']
            if 'tune_ref' not in tune_args:
                tune_args['tune_ref'] = prep_args.get('valid_ref', prep_args['valid_tgt'])
            tune_args['max_len'] = max_len
            best_params, tuner_args_ext = self.tune_decoder_params(exp=exp, **tune_args)
            dec_args['tune'].update(tuner_args_ext)  # Update the config file with default args
            dec_args.update(best_params)
            dec_args['tune']['tuned'] = True

        if 'tune' in best_params:
            del best_params['tune']

        beam_size = best_params.get('beam_size', 4)
        ensemble: int = best_params.pop('ensemble', 5)
        lp_alpha = best_params.get('lp_alpha', 0.0)
        batch_size = best_params.get('batch_size', self.suggested_batch_size(beam_size))

        dec_args.update(dict(beam_size=beam_size, lp_alpha=lp_alpha, ensemble=ensemble,
                             max_len=max_len, batch_size=batch_size))
        exp.persist_state()  # update the config

        assert step > 0, 'looks like no model is saved or invalid experiment dir'
        test_dir = exp.work_dir / f'test_step{step}_beam{beam_size}_ens{ensemble}_lp{lp_alpha}'
        log.info(f"Test Dir = {test_dir}")
        test_dir.mkdir(parents=True, exist_ok=True)

        decoder = Decoder.new(exp, ensemble=ensemble)
        for name, (orig_src, orig_ref) in suit.items():
            # noinspection PyBroadException
            try:
                orig_src, orig_ref = Path(orig_src).resolve(), Path(orig_ref).resolve()
                src_link = test_dir / f'{name}.src'
                ref_link = test_dir / f'{name}.ref'
                for link, orig in [(src_link, orig_src), (ref_link, orig_ref)]:
                    if not link.exists():
                        link.symlink_to(orig)
                out_file = test_dir / f'{name}.out.tsv'
                score = self.decode_eval_file(decoder, src_link, out_file, ref_link,
                                              batch_size=batch_size, beam_size=beam_size,
                                              lp_alpha=lp_alpha)
            except Exception as e:
                log.exception(f"Something went wrong with '{name}' test")
                err = test_dir / f'{name}.err'
                err.write_text(str(e))

    def run(self):
        log.update_file_handler(str(self.exp.log_file))
        self.pre_checks()  # fail early, so TG can fix and restart
        self.exp.pre_process()
        self.exp.train()
        with torch.no_grad():
            exp = Experiment(self.exp.work_dir, read_only=True)
            self.run_tests(exp)


def parse_args():
    parser = argparse.ArgumentParser(prog="rtg.prep", description="prepare NMT experiment")
    parser.add_argument("exp", help="Working directory of experiment", type=Path)
    parser.add_argument("conf", type=Path, nargs='?',
                        help="Config File. By default <work_dir>/conf.yml is used")
    args = parser.parse_args()
    conf_file: Path = args.conf if args.conf else args.exp / 'conf.yml'
    assert conf_file.exists()
    return Experiment(args.exp, config=conf_file)


if __name__ == '__main__':
    pipe = Pipeline(exp=parse_args())
    pipe.run()
