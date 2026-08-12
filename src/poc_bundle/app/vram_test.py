#!/usr/bin/env python3
import argparse, time, torch
ap=argparse.ArgumentParser(); ap.add_argument('--allocate-mb',type=int,default=14336); ap.add_argument('--iterations',type=int,default=300); ap.add_argument('--seed',type=int,default=202607); ap.add_argument('--gpu',type=int,default=0); args=ap.parse_args()
torch.manual_seed(args.seed); torch.cuda.set_device(args.gpu)
elems=args.allocate_mb*1024*1024//4
x=torch.empty(elems, device=f'cuda:{args.gpu}', dtype=torch.float32); x.uniform_()
for i in range(args.iterations):
    y=x*1.0001; torch.cuda.synchronize()
print({'ok':True,'allocate_mb':args.allocate_mb,'peak_allocated_mb':torch.cuda.max_memory_allocated(args.gpu)/1024/1024})
