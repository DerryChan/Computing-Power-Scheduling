#!/usr/bin/env python3
import argparse, time, torch
ap=argparse.ArgumentParser(); ap.add_argument('--reserve-mb',type=int,default=20480); ap.add_argument('--duration',type=int,default=600); ap.add_argument('--gpu',type=int,default=0); ap.add_argument('--gemm',action='store_true'); args=ap.parse_args()
torch.cuda.set_device(args.gpu)
elems=args.reserve_mb*1024*1024//4
x=torch.empty(elems, device=f'cuda:{args.gpu}', dtype=torch.float32)
x.fill_(1.0)
t0=time.time()
while time.time()-t0 < args.duration:
    if args.gemm:
        a=x[:1024*1024].view(1024,1024); b=a@a; torch.cuda.synchronize()
    else:
        time.sleep(1)
print({'ok':True,'reserve_mb':args.reserve_mb,'duration':args.duration})
