"""Preview literal BLI generation masks and exact fold frames without GPU work.

Pass your bare binder on the command line. This example never submits jobs.
For a provider-specific longer spacer, pass --linker with its exact sequence.
The default cassette matches the software preset, not a provider order receipt.

To generate through the existing API with fixed sequence segments:
    client.peptides.generate(gene="EGFR", length_range=(40, 60),
        segment_config=config.segment_config((40, 60)), model="v6.5.21", ...)
Pin a scaffold-capable model and inspect its returned resolved provenance.

To submit the reviewed plan through the ordinary authenticated fold API:
    run = plan.submit(client, target_gene="EGFR", diffusion_samples=4, seed=7)
    results = run.job.wait()
Associate results by the actual folded peptide sequence. Use each frame's
binder_mask(returned_chain_sequence) before attributing binder contacts; full
chain scores from the API still include its tag/spacer. Keep frames separate.
"""
import argparse
from dataclasses import asdict
import json

from ligandai import BliConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binder')
    parser.add_argument('--linker', default='GGSGGSGGSGG')
    parser.add_argument('--tag-position', choices=['c_terminal', 'n_terminal'], default='c_terminal')
    args = parser.parse_args()
    config = BliConfig(tag_position=args.tag_position, linker_sequence=args.linker)
    plan = config.fold_plan({'design': args.binder})
    print(json.dumps({'segment_config': config.segment_config((40, 60)).model_dump(by_alias=True),
        'fold_frames': [asdict(f) | {'sequence_sha256': f.sequence_sha256} for f in plan.frames],
        'unique_constructs': len(plan.sequences), 'submitted': False,
        'sensor_geometry_modelled': False}, indent=2))


if __name__ == '__main__':
    main()
