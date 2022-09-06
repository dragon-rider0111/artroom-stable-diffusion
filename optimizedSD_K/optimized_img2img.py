import argparse, os, sys, glob, random
import torch
import numpy as np
from random import randint
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext
from einops import rearrange, repeat
from ldm.util import instantiate_from_config
import re
from transformers import logging
import traceback
from handle_errs import process_error_trace
from optimUtils import split_weighted_subprompts, logger

logging.set_verbosity_error()

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    return sd

def load_img(path, h0, w0):
   
    image = Image.open(path).convert("RGB")
    w, h = image.size

    print(f"loaded input image of size ({w}, {h}) from {path}")   
    if(h0 is not None and w0 is not None):
        h, w = h0, w0
    
    w, h = map(lambda x: x - x % 64, (w, h))  # resize to integer multiple of 32

    print(f"New image size ({w}, {h})")
    image = image.resize((w, h), resample = Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

config = "optimizedSD/v1-inference-basu.yaml"
device = "cuda"

parser = argparse.ArgumentParser()

parser.add_argument(
    "--prompt",
    type=str,
    nargs="?",
    default="a painting of a virus monster playing guitar",
    help="the prompt to render"
)
parser.add_argument(
    "--outdir",
    type=str,
    nargs="?",
    help="dir to write results to",
    default="outputs/img2img-samples"
)

parser.add_argument(
    "--init-img",
    type=str,
    nargs="?",
    help="path to the input image"
)

parser.add_argument(
    "--skip_grid",
    action='store_true',
    help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
)
parser.add_argument(
    "--skip_save",
    action='store_true',
    help="do not save individual samples. For speed measurements.",
)
parser.add_argument(
    "--ddim_steps",
    type=int,
    default=50,
    help="number of ddim sampling steps",
)

parser.add_argument(
    "--ddim_eta",
    type=float,
    default=0.0,
    help="ddim eta (eta=0.0 corresponds to deterministic sampling",
)
parser.add_argument(
    "--n_iter",
    type=int,
    default=1,
    help="sample this often",
)
parser.add_argument(
    "--H",
    type=int,
    default=None,
    help="image height, in pixel space",
)
parser.add_argument(
    "--W",
    type=int,
    default=None,
    help="image width, in pixel space",
)
parser.add_argument(
    "--strength",
    type=float,
    default=0.75,
    help="strength for noising/unnoising. 1.0 corresponds to full destruction of information in init image",
)
parser.add_argument(
    "--C",
    type=int,
    default=4,
    help="latent channels",
)
parser.add_argument(
    "--f",
    type=int,
    default=8,
    help="downsampling factor",
)
parser.add_argument(
    "--n_samples",
    type=int,
    default=5,
    help="how many samples to produce for each given prompt. A.k.a. batch size",
)
parser.add_argument(
    "--n_rows",
    type=int,
    default=0,
    help="rows in the grid (default: n_samples)",
)
parser.add_argument(
    "--scale",
    type=float,
    default=7.5,
    help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
)
parser.add_argument(
    "--from-file",
    type=str,
    help="if specified, load prompts from this file",
)
parser.add_argument(
    "--seed",
    type=int,
    default=None,
    help="the seed (for reproducible sampling)",
)
parser.add_argument(
    "--small_batch",
    action='store_true',
    help="Reduce inference time when generate a smaller batch of images",
)
parser.add_argument(
    "--precision",
    type=str,
    help="evaluate at this precision",
    choices=["full", "autocast"],
    default="autocast"
)
parser.add_argument(
    "--ckpt",
    type=str,
    help="path to checkpoint of model",
)
parser.add_argument(
    "--turbo",
    action="store_true",
    help="Reduces inference time on the expense of 1GB VRAM",
)
opt = parser.parse_args()
ckpt = opt.ckpt

tic = time.time()
os.makedirs(opt.outdir, exist_ok=True)
outpath = opt.outdir

if opt.seed == None:
    opt.seed = randint(0, 1000000)
print("init_seed = ", opt.seed)
seed_everything(opt.seed)

sd = load_model_from_config(f"{ckpt}")
li = []
lo = []
for key, value in sd.items():
    sp = key.split('.')
    if(sp[0]) == 'model':
        if('input_blocks' in sp):
            li.append(key)
        elif('middle_block' in sp):
            li.append(key)
        elif('time_embed' in sp):
            li.append(key)
        else:
            lo.append(key)
for key in li:
    sd['model1.' + key[6:]] = sd.pop(key)
for key in lo:
    sd['model2.' + key[6:]] = sd.pop(key)

config = OmegaConf.load(f"{config}")
config.modelUNet.params.ddim_steps = opt.ddim_steps

if opt.small_batch:
    config.modelUNet.params.small_batch = True
else:
    config.modelUNet.params.small_batch = False

assert os.path.isfile(opt.init_img)
init_image = load_img(opt.init_img, opt.H, opt.W).to(device)

model = instantiate_from_config(config.modelUNet)
_, _ = model.load_state_dict(sd, strict=False)
model.eval()
model.turbo = opt.turbo
    
modelCS = instantiate_from_config(config.modelCondStage)
_, _ = modelCS.load_state_dict(sd, strict=False)
modelCS.eval()
    
modelFS = instantiate_from_config(config.modelFirstStage)
_, _ = modelFS.load_state_dict(sd, strict=False)
modelFS.eval()

if opt.precision == "autocast":
    model.half()
    modelCS.half()
    modelFS.half()
    init_image = init_image.half()

batch_size = opt.n_samples*opt.n_iter 
opt.n_iter = 1

n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
if not opt.from_file:
    prompt = opt.prompt
    print("Prompt:",opt.prompt)
    assert prompt is not None
    data = [batch_size * [prompt]]

else:
    # print(f"reading prompts from {opt.from_file}")
    with open(opt.from_file, "r") as f:
        opt.prompt = f.read().splitlines()[0]
        print("Prompt:",opt.prompt)
        # data = list(chunk(opt.prompt, batch_size))
        data = [batch_size * [opt.prompt]]

sample_path = os.path.join(outpath,re.sub(r'\W+', '',"_".join(opt.prompt.split())))[:150]
os.makedirs(sample_path, exist_ok=True)
base_count = len(os.listdir(sample_path))
grid_count = len(os.listdir(outpath)) - 1

modelFS.to(device)

try:
    init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
    init_latent = modelFS.get_first_stage_encoding(modelFS.encode_first_stage(init_image))  # move to latent space

    mem = torch.cuda.memory_allocated()/1e6
    modelFS.to("cpu")
    while(torch.cuda.memory_allocated()/1e6 >= mem):
        time.sleep(1)


    assert 0. <= opt.strength <= 1., 'can only work with strength in [0.0, 1.0]'
    t_enc = int(opt.strength * opt.ddim_steps)
    print(f"target t_enc is {t_enc} steps")


    precision_scope = autocast if opt.precision=="autocast" else nullcontext


    with torch.no_grad():
        all_samples = list()
        for n in trange(opt.n_iter, desc="Sampling"):
            for prompts in tqdm(data, desc="data"):
                with precision_scope("cuda"):
                    modelCS.to(device)
                    uc = None
                    if opt.scale != 1.0:
                        uc = modelCS.get_learned_conditioning(batch_size * [""])
                    if isinstance(prompts, tuple):
                        prompts = list(prompts)
                    
                    c = modelCS.get_learned_conditioning(prompts)
                    mem = torch.cuda.memory_allocated()/1e6
                    modelCS.to("cpu")
                    while(torch.cuda.memory_allocated()/1e6 >= mem):
                        time.sleep(1)

                    # encode (scaled latent)
                    z_enc = model.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(device), opt.seed)
                    # decode it
                    samples_ddim = model.decode(z_enc, c, t_enc, unconditional_guidance_scale=opt.scale,
                                                unconditional_conditioning=uc,)


                    modelFS.to(device)
                    print("saving images")
                    for i in range(batch_size):    
                        x_samples_ddim = modelFS.decode_first_stage(samples_ddim[i].unsqueeze(0))
                        x_sample = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        x_sample = 255. * rearrange(x_sample[0].cpu().numpy(), 'c h w -> h w c')
                        Image.fromarray(x_sample.astype(np.uint8)).save(
                            os.path.join(sample_path, "seed_" + str(opt.seed) + "_" + f"{base_count:05}.png"))
                        Image.fromarray(x_sample.astype(np.uint8)).save(
                            os.path.join(sample_path, "latest.png"))
                        base_count += 1
                        opt.seed += 1

                    mem = torch.cuda.memory_allocated()/1e6
                    modelFS.to("cpu")
                    while(torch.cuda.memory_allocated()/1e6 >= mem):
                        time.sleep(1)

                    del samples_ddim
                    print("memory_final = ", torch.cuda.memory_allocated()/1e6)
except Exception as err:
    print(opt.from_file)
    process_error_trace(traceback.format_exc(), err, opt.from_file)

toc = time.time()

time_taken = (toc-tic)/60.0

print(("Your samples are ready in {0:.2f} minutes and waiting for you here \n" + sample_path).format(time_taken))