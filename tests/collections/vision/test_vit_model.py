# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

import pytest
import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer

from nemo.collections.vision.models.megatron_vit_classification_models import MegatronVitClassificationModel
from nemo.collections.vision.data.megatron.vit_dataset import build_train_valid_datasets
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy

DEVICE_CAPABILITY = None
if torch.cuda.is_available():
    DEVICE_CAPABILITY = torch.cuda.get_device_capability()


@pytest.fixture()
def model_cfg():

    model_cfg_string = """
      precision: ${trainer.precision}
      micro_batch_size: 256 # limited by GPU memory
      global_batch_size: 4096 # will use more micro batches to reach global batch size
      tensor_model_parallel_size: 1 # intra-layer model parallelism
      pipeline_model_parallel_size: 1 # inter-layer model parallelism
      virtual_pipeline_model_parallel_size: null # interleaved pipeline
    
      restore_from_pretrained: null # used in fine-tuning
    
      # vision configs
      vision_pretraining_type: "classify"
      num_classes: 1000
      patch_dim: 16
      img_h: 224
      img_w: 224
      classes_fraction: 1.0
      data_per_class_fraction: 1.0
      num_channels: 3
      drop_path_rate: 0.0
    
      # model architecture
      encoder_seq_length: 4
      max_position_embeddings: ${.encoder_seq_length}
      num_layers: 12
      hidden_size: 768
      ffn_hidden_size: 3072 # Transformer FFN hidden size. Usually 4 * hidden_size.
      num_attention_heads: 12
      init_method_std: 0.02 # Standard deviation of the zero mean normal distribution used for weight initialization.')
      use_scaled_init_method: True # use scaled residuals initialization
      hidden_dropout: 0.1 # Dropout probability for hidden state transformer.
      attention_dropout: 0.
      kv_channels: null # Projection weights dimension in multi-head attention. Set to hidden_size // num_attention_heads if null
      apply_query_key_layer_scaling: True # scale Q * K^T by 1 / layer-number.
      normalization: layernorm # Type of normalization layers
      layernorm_epsilon: 1e-5
      do_layer_norm_weight_decay: False # True means weight decay on all params
      pre_process: True # add embedding
      post_process: True # add pooler
      persist_layer_norm: True # Use of persistent fused layer norm kernel.
    
      # precision
      native_amp_init_scale: 4294967296 # 2 ** 32
      native_amp_growth_interval: 1000
      hysteresis: 2 # Gradient scale hysteresis
      fp32_residual_connection: False # Move residual connections to fp32
      fp16_lm_cross_entropy: False # Move the cross entropy unreduced loss calculation for lm head to fp16
    
      # Megatron O2-style half-precision
      megatron_amp_O2: False # Enable O2-level automatic mixed precision using main parameters
      grad_allreduce_chunk_size_mb: 125
      grad_div_ar_fusion: True # Fuse grad division into torch.distributed.all_reduce
      masked_softmax_fusion: True # Use a kernel that fuses the attention softmax with it's mask.
      bias_dropout_add_fusion: True # Use a kernel that fuses the bias addition, dropout and residual connection addition.
    
      # miscellaneous
      seed: 1234
      resume_from_checkpoint: null # manually set the checkpoint file to load from
      use_cpu_initialization: False # Init weights on the CPU (slow for large models)
      onnx_safe: False # Use work-arounds for known problems with Torch ONNX exporter.
      apex_transformer_log_level: 30 # Python logging level displays logs with severity greater than or equal to this
      gradient_as_bucket_view: True # PyTorch DDP argument. Allocate gradients in a contiguous bucket to save memory (less fragmentation and buffer memory)
      gradient_accumulation_fusion: False # Fuse weight gradient accumulation to GEMMs. Only used with pipeline parallelism.
      openai_gelu: False
      bias_gelu_fusion: False
      megatron_legacy: False
    
      ## Activation Checkpointing
      # NeMo Megatron supports 'selective' activation checkpointing where only the memory intensive part of attention is checkpointed.
      # These memory intensive activations are also less compute intensive which makes activation checkpointing more efficient for LLMs (20B+).
      # See Reducing Activation Recomputation in Large Transformer Models: https://arxiv.org/abs/2205.05198 for more details.
      # 'full' will checkpoint the entire transformer layer.
      activations_checkpoint_granularity: null # 'selective' or 'full' 
      activations_checkpoint_method: null # 'uniform', 'block', not used with 'selective'
      # 'uniform' divides the total number of transformer layers and checkpoints the input activation
      # of each chunk at the specified granularity
      # 'block' checkpoints the specified number of layers per pipeline stage at the specified granularity
      activations_checkpoint_num_layers: null # not used with 'selective'
      # when using 'uniform' this creates groups of transformer layers to checkpoint. Usually set to 1. Increase to save more memory.
      # when using 'block' this this will checkpoint the first activations_checkpoint_num_layers per pipeline stage.
    
      ## Sequence Parallelism
      # Makes tensor parallelism more memory efficient for LLMs (20B+) by parallelizing layer norms and dropout sequentially
      # See Reducing Activation Recomputation in Large Transformer Models: https://arxiv.org/abs/2205.05198 for more details.
      sequence_parallel: False
    
      data:
        # Path to image dataset must be specified by the user.
        # Supports List
        # List: can override from the CLI: "model.data.data_prefix=[/path/to/train, /path/to/val]",
        data_path: "dummy/path"
        num_workers: 2
        dataloader_type: cyclic # cyclic
        validation_drop_last: True # Set to false if the last partial validation samples is to be consumed
        data_sharding: False
    
      # Nsys profiling options
      nsys_profile:
        enabled: False
        start_step: 10  # Global batch to start profiling
        end_step: 10 # Global batch to end profiling
        ranks: [0] # Global rank IDs to profile
        gen_shape: False # Generate model and kernel details including input shapes
      
      optim:
        name: fused_adam
        lr: 5e-4
        weight_decay: 0.1
        betas: 
        - 0.9
        - 0.999
        sched:
          name: CosineAnnealing
          warmup_steps: 10000
          constant_steps: 0
          min_lr: 1e-5
    """
    model_cfg = OmegaConf.create(model_cfg_string)
    return model_cfg


@pytest.fixture()
def trainer_cfg():

    trainer_cfg_string = """
        devices: 1
        num_nodes: 1
        accelerator: gpu
        precision: 16
        logger: False
        enable_checkpointing: False
        replace_sampler_ddp: False
        max_epochs: -1
        max_steps: 95000
        log_every_n_steps: 10
        val_check_interval: 100
        limit_val_batches: 50
        limit_test_batches: 500
        accumulate_grad_batches: 1
        gradient_clip_val: 1.0
        benchmark: False
        enable_model_summary: False
    """
    trainer_cfg = OmegaConf.create(trainer_cfg_string)

    return trainer_cfg


@pytest.fixture()
def precision():
    return 32


@pytest.fixture()
def vit_classification_model(model_cfg, trainer_cfg, precision):
    model_cfg['precision'] = precision
    trainer_cfg['precision'] = precision

    strategy = NLPDDPStrategy()

    trainer = Trainer(strategy=strategy, **trainer_cfg)

    cfg = DictConfig(model_cfg)

    model = MegatronVitClassificationModel(cfg=cfg, trainer=trainer)

    return model

@pytest.mark.run_only_on('GPU')
class TestMegatronVitClassificationModel:
    @pytest.mark.unit
    def test_constructor(self, vit_classification_model):
        assert isinstance(vit_classification_model, MegatronVitClassificationModel)

        num_weights = vit_classification_model.num_weights
        assert num_weights == 87169000

    @pytest.mark.unit
    def test_build_dataset(self, vit_classification_model, test_data_dir):
        data_path = [
            os.path.join(test_data_dir, "vision/tiny_imagenet/train"),
            os.path.join(test_data_dir, "vision/tiny_imagenet/val"),
        ]
        train_ds, validation_ds = build_train_valid_datasets(
            model_cfg=vit_classification_model.cfg,
            data_path=data_path,
            image_size=(vit_classification_model.cfg.img_h, vit_classification_model.cfg.img_w),
        )
        assert len(train_ds) == 20
        assert len(validation_ds) == 20
        assert list(train_ds[0][0].shape) == [3, 224, 224]
        assert list(validation_ds[0][0].shape) == [3, 224, 224]


    @pytest.mark.parametrize(
        "precision",
        [
            32,
            16,
            pytest.param(
                "bf16",
                marks=pytest.mark.skipif(
                    not DEVICE_CAPABILITY or DEVICE_CAPABILITY[0] < 8,
                    reason='bfloat16 is not supported on this device',
                ),
            ),
        ],
    )

    @pytest.mark.unit
    def test_forward(self, vit_classification_model, test_data_dir, precision):

        dtype = None
        if vit_classification_model.cfg['precision'] == 32:
            dtype = torch.float
        elif vit_classification_model.cfg['precision'] == 16:
            dtype = torch.float16
        elif vit_classification_model.cfg['precision'] == 'bf16':
            dtype = torch.bfloat16
        else:
            raise ValueError(f"precision: {vit_classification_model.cfg['precision']} is not supported.")

        vit_classification_model.eval()

        data_path = [
            os.path.join(test_data_dir, "vision/tiny_imagenet/train"),
            os.path.join(test_data_dir, "vision/tiny_imagenet/val"),
        ]
        _, validation_ds = build_train_valid_datasets(
            model_cfg=vit_classification_model.cfg,
            data_path=data_path,
            image_size=(vit_classification_model.cfg.img_h, vit_classification_model.cfg.img_w),
        )
        # shape: (B, C, H, W)
        images = [validation_ds[i][0] for i in range(4)]
        tokens = torch.stack(images, dim=0)

        with torch.no_grad():
            B, C, H, W = tokens.shape
            assert H == W
            with torch.autocast('cuda', dtype=dtype):
                output_tensor = vit_classification_model.forward(
                    tokens=tokens.cuda(),
                )
            # output is (B, #classes)
            assert output_tensor.shape[0] == B
            assert output_tensor.shape[1] == vit_classification_model.cfg['num_classes']
            assert output_tensor.dtype == dtype
