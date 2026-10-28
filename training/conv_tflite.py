# -*- coding: utf-8 -*-
"""从 best_weights 重新导出 streaming 模型 → float tflite → uint8 input 的 int8 quant tflite。

训练后导出的 stream_state_internal(s) 里 Stream 层状态变量是 int8（TF 2.10 的
_experimental_variable_quantization），但板端（esp32 tflite-micro）跑出来概率恒 0。
这里绕开 SavedModel，直接从 Keras 权重重建 streaming 模型再转换：
  1) float 模型 load_weights → to_streaming_inference
  2) float tflite（input float32）
  3) representative dataset（用负样本特征，保证覆盖）→ int8 权重、uint8 输入
量化后输入 int8 / [features/255*2-1]，和固件 streaming_model.cpp 的约定一致。
"""
import os
import sys

import numpy as np
import tensorflow as tf
import yaml

sys.path.insert(0, 'D:/mww/microWakeWord')
from microwakeword import mixednet, utils

from microwakeword.data import FeatureHandler
from microwakeword.layers import modes

TRAIN_DIR = sys.argv[1] if len(sys.argv) > 1 else 'D:/mww/trained_models/xiaochen_r8'
TRAIN_DIR = TRAIN_DIR.replace('\\', '/')
OUT_FLOAT = os.path.join(TRAIN_DIR, 'export_float.tflite')
OUT_QUANT = os.path.join(TRAIN_DIR, 'export_quant_int8.tflite')


def build_config():
    from microwakeword import model_train_eval
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='model_name')
    mixednet.model_parameters(sub.add_parser('mixednet'))
    flags = ap.parse_args(['mixednet'])
    # 和实际训练一致（training_parameters.yaml 里没写的用 mixednet 默认）
    flags.pointwise_filters = '64,64,64,64'
    flags.repeat_in_block = '1,1,1,1'
    flags.mixconv_kernel_sizes = '[5],[7,11],[9,15],[23]'
    flags.residual_connection = '0,0,0,0'
    flags.first_conv_filters = 32
    flags.first_conv_kernel_size = 5
    flags.max_pool = 0
    flags.pooled = 0
    flags.spatial_attention = 0
    flags.stride = 3
    cfg = yaml.load(open(os.path.join(TRAIN_DIR, 'training_config.yaml'), encoding='utf8').read(), yaml.Loader)
    cfg['train_dir'] = TRAIN_DIR
    cfg['summaries_dir'] = os.path.join(TRAIN_DIR, 'logs/')
    cfg['window_step_ms'] = cfg.get('window_step_ms', 10)
    desired = int(16000 * cfg['clip_duration_ms'] / 1000)
    step = int(cfg['stride'] * 16000 * cfg['window_step_ms'] / 1000)
    cfg['spectrogram_length_final_layer'] = 1 + int((desired - 480) / step)
    cfg['spectrogram_length'] = cfg['spectrogram_length_final_layer'] + mixednet.spectrogram_slices_dropped(flags)
    cfg['stride'] = flags.stride
    cfg['training_input_shape'] = modes.get_input_data_shape(cfg, modes.Modes.TRAINING)
    cfg['flags'] = flags
    return cfg, flags


def main(weights='best_weights.weights.h5'):
    cfg, flags = build_config()
    # 训练时的 batch 是 64；这里只要有数据进模型即可
    model = mixednet.model(flags, shape=cfg['training_input_shape'], batch_size=1)
    model.load_weights(os.path.join(TRAIN_DIR, weights))
    model.summary()

    stream = utils.model_to_saved(model, cfg, mode=modes.Modes.STREAM_INTERNAL_STATE_INFERENCE)
    stream.save(os.path.join(TRAIN_DIR, 'export_stream'), save_format='tf')

    # 1) float tflite
    conv = tf.lite.TFLiteConverter.from_saved_model(os.path.join(TRAIN_DIR, 'export_stream'))
    conv._experimental_variable_quantization = True
    open(OUT_FLOAT, 'wb').write(conv.convert())
    print('wrote', OUT_FLOAT, os.path.getsize(OUT_FLOAT))

    # 2) 代表样本：负样本特征（比正样本更能覆盖每层激活范围）
    fh = FeatureHandler(cfg)
    feats, _, _ = fh.get_data('training', 500, features_length=cfg['spectrogram_length'])
    stride = cfg['stride']

    def rep():
        feats[0][0, 0] = 0.0
        feats[0][0, 1] = 26.0
        for spec in feats:
            for i in range(0, spec.shape[0] - stride, stride):
                yield [spec[i:i + stride, :].astype(np.float32)]

    conv = tf.lite.TFLiteConverter.from_saved_model(os.path.join(TRAIN_DIR, 'export_stream'))
    conv.optimizations = {tf.lite.Optimize.DEFAULT}
    conv._experimental_variable_quantization = True
    conv.target_spec.supported_ops = {tf.lite.OpsSet.TFLITE_BUILTINS_INT8}
    # ESPHome micro_wake_word 2025.7 只接受 int8 输入（uint8 会直接报错）
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.uint8
    conv.representative_dataset = tf.lite.RepresentativeDataset(rep)
    open(OUT_QUANT, 'wb').write(conv.convert())
    print('wrote', OUT_QUANT, os.path.getsize(OUT_QUANT))


if __name__ == '__main__':
    main(*sys.argv[2:])
