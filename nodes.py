import os
import torch
import json
from torchvision.transforms import v2
from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device

import folder_paths
import comfy.model_management as mm
from comfy.utils import load_torch_file

script_directory = os.path.dirname(os.path.abspath(__file__))

if not "mmaudio" in folder_paths.folder_names_and_paths:
    folder_paths.add_model_folder_path("mmaudio", os.path.join(folder_paths.models_dir, "mmaudio"))


from .mmaudio.eval_utils import generate
from .mmaudio.model.flow_matching import FlowMatching
from .mmaudio.model.networks import MMAudio
from .mmaudio.model.utils.features_utils import FeaturesUtils
from .mmaudio.model.sequence_config import (CONFIG_16K, CONFIG_44K, SequenceConfig)
from .mmaudio.ext.bigvgan_v2.bigvgan import BigVGAN as BigVGANv2
from .mmaudio.ext.synchformer import Synchformer
from .mmaudio.ext.autoencoder import AutoEncoderModule
from open_clip import CLIP

import logging
import torch.nn.functional as F
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def resample_video(video: torch.Tensor, new_frames: int) -> torch.Tensor:
    if video.shape[0] == new_frames:
        return video
    # video: [t, h, w, c]
    # To [1, c, t, h, w]
    video = video.permute(3, 0, 1, 2).unsqueeze(0)  # [1, c, t, h, w]
    # Interpolate temporally
    video = F.interpolate(video, size=(new_frames, video.shape[3], video.shape[4]), mode='trilinear', align_corners=False)
    # Back to [t, h, w, c]
    video = video.squeeze(0).permute(1, 2, 3, 0)
    return video

def process_video_tensor(video_tensor: torch.Tensor, duration_sec: float) -> tuple[torch.Tensor, torch.Tensor, float]:
    _CLIP_SIZE = 384
    _CLIP_FPS = 8.0

    _SYNC_SIZE = 224
    _SYNC_FPS = 25.0

    clip_transform = v2.Compose([
        v2.Resize((_CLIP_SIZE, _CLIP_SIZE), interpolation=v2.InterpolationMode.BICUBIC),
        v2.ToPILImage(),
        v2.ToTensor(),
        v2.ConvertImageDtype(torch.float32),
    ])

    sync_transform = v2.Compose([
        v2.Resize(_SYNC_SIZE, interpolation=v2.InterpolationMode.BICUBIC),
        v2.CenterCrop(_SYNC_SIZE),
        v2.ToPILImage(),
        v2.ToTensor(),
        v2.ConvertImageDtype(torch.float32),
        v2.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ])

    # Assuming video_tensor is in the shape (frames, height, width, channels)
    total_frames = video_tensor.shape[0]
    clip_frames_count = int(_CLIP_FPS * duration_sec)
    sync_frames_count = int(_SYNC_FPS * duration_sec)

    # Resample to required frame counts
    clip_video_tensor = resample_video(video_tensor, clip_frames_count)
    sync_video_tensor = resample_video(video_tensor, sync_frames_count)

    clip_frames = clip_video_tensor.permute(0, 3, 1, 2)
    sync_frames = sync_video_tensor.permute(0, 3, 1, 2)

    clip_frames = torch.stack([clip_transform(frame) for frame in clip_frames])
    sync_frames = torch.stack([sync_transform(frame) for frame in sync_frames])

    # No need to truncate duration, as we resampled to match

    return clip_frames, sync_frames, duration_sec

#region Model loading
class MMAudioModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mmaudio_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from the 'ComfyUI/models/mmaudio' -folder",}),
            
            "base_precision": (["fp16", "fp32", "bf16"], {"default": "fp16"}),
            },
        }

    RETURN_TYPES = ("MMAUDIO_MODEL",)
    RETURN_NAMES = ("mmaudio_model", )
    FUNCTION = "loadmodel"
    CATEGORY = "MMAudio"

    def loadmodel(self, mmaudio_model, base_precision):
       

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
       
        mm.soft_empty_cache()

        base_dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[base_precision]

        mmaudio_model_path = folder_paths.get_full_path_or_raise("mmaudio", mmaudio_model)
        mmaudio_sd = load_torch_file(mmaudio_model_path, device=offload_device)

        if "small" in mmaudio_model:
            num_heads = 7
            model = MMAudio(
                    latent_dim=40,
                    clip_dim=1024,
                    sync_dim=768,
                    text_dim=1024,
                    hidden_dim=64 * num_heads,
                    depth=12,
                    fused_depth=8,
                    num_heads=num_heads,
                    latent_seq_len=345,
                    clip_seq_len=64,
                    sync_seq_len=192
                    )
        elif "large" in mmaudio_model:
            num_heads = 14
            model = MMAudio(latent_dim=40,
                    clip_dim=1024,
                    sync_dim=768,
                    text_dim=1024,
                    hidden_dim=64 * num_heads,
                    depth=21,
                    fused_depth=14,
                    num_heads=num_heads,
                    latent_seq_len=345,
                    clip_seq_len=64,
                    sync_seq_len=192,
                    v2=True
                    )
        model = model.eval().to(device=device, dtype=base_dtype)
        model.load_weights(mmaudio_sd)
        log.info(f'Loaded MMAudio model weights from {mmaudio_model_path}')
        if "44" in mmaudio_model:
            model.seq_cfg = CONFIG_44K
        elif "16" in mmaudio_model:
            model.seq_cfg = CONFIG_16K
        
       
        return (model,)
    
#region Features Utils
class MMAudioVoCoderLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "vocoder_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),  
            },
        }

    RETURN_TYPES = ("VOCODER_MODEL",)
    RETURN_NAMES = ("mmaudio_vocoder", )
    FUNCTION = "loadmodel"
    CATEGORY = "MMAudio"

    def loadmodel(self, vocoder_model):
        from .mmaudio.ext.bigvgan import BigVGAN
        vocoder_model_path = folder_paths.get_full_path_or_raise("mmaudio", vocoder_model)
        vocoder_model = BigVGAN.from_pretrained(vocoder_model_path).eval()
        return (vocoder_model_path,)
        
class MMAudioFeatureUtilsLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "vae_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
                "synchformer_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
                "clip_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
            },
            "optional": {
              "bigvgan_vocoder_model": ("VOCODER_MODEL", {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
                "mode": (["16k", "44k"], {"default": "44k"}),
                "precision": (["fp16", "fp32", "bf16"],
                    {"default": "fp16"}
                ),
            }
        }

    RETURN_TYPES = ("MMAUDIO_FEATUREUTILS",)
    RETURN_NAMES = ("mmaudio_featureutils", )
    FUNCTION = "loadmodel"
    CATEGORY = "MMAudio"

    def loadmodel(self, vae_model, precision, synchformer_model, clip_model, mode, bigvgan_vocoder_model=None):
        
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        #synchformer
        synchformer_path = folder_paths.get_full_path_or_raise("mmaudio", synchformer_model)
        synchformer_sd = load_torch_file(synchformer_path, device=offload_device)
        synchformer = Synchformer()
        synchformer.load_state_dict(synchformer_sd)
        synchformer = synchformer.eval().to(device=device, dtype=dtype)

        #vae
        download_path = folder_paths.get_folder_paths("mmaudio")[0]

        nvidia_bigvgan_vocoder_path = os.path.join(download_path, "nvidia", "bigvgan_v2_44khz_128band_512x")
        if mode == "44k":
            if not os.path.exists(nvidia_bigvgan_vocoder_path):
                log.info(f"Downloading nvidia bigvgan vocoder model to: {nvidia_bigvgan_vocoder_path}")
                from huggingface_hub import snapshot_download

                snapshot_download(
                    repo_id="nvidia/bigvgan_v2_44khz_128band_512x",
                    ignore_patterns=["*3m*",],
                    local_dir=nvidia_bigvgan_vocoder_path,
                    local_dir_use_symlinks=False,
                )
            
            bigvgan_vocoder = BigVGANv2.from_pretrained(nvidia_bigvgan_vocoder_path).eval().to(device=device, dtype=dtype)
        else:
            assert bigvgan_vocoder_model is not None, "bigvgan_vocoder_model must be provided for 16k mode"
            bigvgan_vocoder = bigvgan_vocoder_model
        
        vae_path = folder_paths.get_full_path_or_raise("mmaudio", vae_model)
        vae_sd = load_torch_file(vae_path, device=offload_device)
        vae = AutoEncoderModule(
            vae_state_dict=vae_sd,
            bigvgan_vocoder=bigvgan_vocoder,
            mode=mode
            )
        vae = vae.eval().to(device=device, dtype=dtype)

        #clip
       
        clip_model_path = folder_paths.get_full_path_or_raise("mmaudio", clip_model)
        clip_config_path = os.path.join(script_directory, "configs", "DFN5B-CLIP-ViT-H-14-384.json")
        with open(clip_config_path) as f:
             clip_config = json.load(f)
            
        with init_empty_weights():
            clip_model = CLIP(**clip_config["model_cfg"]).eval()
        clip_sd = load_torch_file(os.path.join(clip_model_path), device=offload_device)
        for name, param in clip_model.named_parameters():
            set_module_tensor_to_device(clip_model, name, device=device, dtype=dtype, value=clip_sd[name])
        clip_model.to(device=device, dtype=dtype)

        #clip_model = create_model_from_pretrained("hf-hub:apple/DFN5B-CLIP-ViT-H-14-384", return_transform=False)
        
       
        feature_utils = FeaturesUtils(vae=vae,
                                  synchformer=synchformer,
                                  enable_conditions=True,
                                  clip_model=clip_model)
        return (feature_utils,)

#region sampling
class MMAudioSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mmaudio_model": ("MMAUDIO_MODEL",),
                "feature_utils": ("MMAUDIO_FEATUREUTILS",),
                "duration": ("FLOAT", {"default": 8, "step": 0.01, "tooltip": "Duration of the audio in seconds"}),
                "steps": ("INT", {"default": 25, "step": 1, "tooltip": "Number of steps to interpolate"}),
                "cfg": ("FLOAT", {"default": 4.5, "step": 0.1, "tooltip": "Strength of the conditioning"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "prompt": ("STRING", {"default": "", "multiline": True} ),
                "negative_prompt": ("STRING", {"default": "", "multiline": True} ),
                "mask_away_clip": ("BOOLEAN", {"default": False, "tooltip": "If true, the clip video will be masked away"}),
                "force_offload": ("BOOLEAN", {"default": True, "tooltip": "If true, the model will be offloaded to the offload device"}),
            },
            "optional": {
                "images": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio", )
    FUNCTION = "sample"
    CATEGORY = "MMAudio"

    def sample(self, mmaudio_model, seed, feature_utils, duration, steps, cfg, prompt, negative_prompt, mask_away_clip, force_offload, images=None):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        rng = torch.Generator(device=device)
        rng.manual_seed(seed)

        seq_cfg = mmaudio_model.seq_cfg
       
        if images is not None:
            images = images.to(device=device)
            clip_frames, sync_frames, duration = process_video_tensor(images, duration)
            print("clip_frames", clip_frames.shape, "sync_frames", sync_frames.shape, "duration", duration)
            if mask_away_clip:
                clip_frames = None
            else:
                clip_frames = clip_frames.unsqueeze(0)
            sync_frames = sync_frames.unsqueeze(0)
        else:
            clip_frames = None
            sync_frames = None
        
        seq_cfg.duration = duration
        mmaudio_model.update_seq_lengths(seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len)

        scheduler = FlowMatching(min_sigma=0, inference_mode='euler', num_steps=steps)
        feature_utils.to(device)
        mmaudio_model.to(device)
        audios = generate(clip_frames,
                      sync_frames, [prompt],
                      negative_text=[negative_prompt],
                      feature_utils=feature_utils,
                      net=mmaudio_model,
                      fm=scheduler,
                      rng=rng,
                      cfg_strength=cfg)
        if force_offload:
            mmaudio_model.to(offload_device)
            feature_utils.to(offload_device)
            mm.soft_empty_cache()
        waveform = audios.float().cpu()
        #torchaudio.save("test.wav", waveform, 44100)
        audio = {
            "waveform": waveform,
            "sample_rate": 44100
        }

        return (audio,)
        
NODE_CLASS_MAPPINGS = {
    "MMAudioModelLoader": MMAudioModelLoader,
    "MMAudioFeatureUtilsLoader": MMAudioFeatureUtilsLoader,
    "MMAudioSampler": MMAudioSampler,
    "MMAudioVoCoderLoader": MMAudioVoCoderLoader,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MMAudioModelLoader": "MMAudio ModelLoader",
    "MMAudioFeatureUtilsLoader": "MMAudio FeatureUtilsLoader",
    "MMAudioSampler": "MMAudio Sampler",
    "MMAudioVoCoderLoader": "MMAudio VoCoderLoader",
    }
