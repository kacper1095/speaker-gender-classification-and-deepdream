import os
from functools import partial
from typing import Iterable, Optional, Tuple

import cv2
import librosa
import numpy as np
import torch
import torch.nn as nn
import tqdm
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline
from skorch.net import NeuralNet

import constants
from dataset import ExtractStft
from model import Classifier
from pytorch_extensions import roll


class DataLoader(TransformerMixin, BaseEstimator):
    def fit(self, x, y, **fit_params):
        return self

    @classmethod
    def transform(cls, x: Iterable[str]) -> Iterable[Tuple[np.ndarray, int]]:
        output = []
        for file_name in x:
            signal, sample_rate = librosa.load(file_name, sr=constants.LIBRISPEECH_SAMPLE_RATE)
            output.append((signal, sample_rate))
        return output


class DataPreprocessor(TransformerMixin, BaseEstimator):
    def fit(self, x, y, **fit_params):
        return self

    @classmethod
    def transform(cls, x: Iterable[Tuple[np.ndarray, int]]) -> Iterable[Tuple[np.ndarray, int, float, np.ndarray]]:
        output = []
        for signal, sample_rate in x:
            stacked, phase, mag_max_value = ExtractStft.get_stft(signal)
            output.append((stacked, sample_rate, mag_max_value, phase))
        return output


class Model(TransformerMixin, BaseEstimator):
    def __init__(self,
                 model_path: str,
                 block_name: str,
                 number_of_iterations: int,
                 optimisation_step_size: float = 1.5,
                 n_octaves: int = 4,
                 octave_scale: float = 1.4,
                 use_gpu: bool = False,
                 verbose: bool = False,
                 seed: Optional[int] = None):
        self._model_path = model_path
        self._use_gpu = use_gpu
        self._block_name = block_name
        self._n_octaves = n_octaves
        self._octave_scale = octave_scale
        self._verbose = verbose
        self._number_of_iterations = number_of_iterations
        self._np_rng = np.random.RandomState(seed)
        self._optimisation_step_size = optimisation_step_size

        self._available_layers = {}
        self._available_layers_names = []

        self._classifier = Classifier(constants.NUMBER_OF_CLASSES)
        self._net = NeuralNet(
            self._classifier, nn.CrossEntropyLoss
        )
        self._net.initialize()
        self._net.load_params(f_params=self._model_path)
        self._classifier = self._classifier.eval()

        for layer_name, layer in self._classifier.layers_blocks.items():
            if "residual" in layer_name:
                current_register = partial(self._register_layer_output, layer_name=layer_name)
                layer.register_forward_hook(current_register)
                self._available_layers_names.append(layer_name)

        if self._verbose:
            print(f"Available layer names: \n{self._available_layers_names}")

    def _register_layer_output(self, module, input_, output, layer_name):
        self._available_layers[layer_name] = output

    def fit(self, x, y, **fit_params):
        return self

    def transform(self, x: Iterable[Tuple[np.ndarray, int, float, np.ndarray]]) \
            -> Iterable[Tuple[np.ndarray, int, float, np.ndarray]]:
        output = []
        for stft, sample_rate, mag_max_value, phase in x:
            prediction = self._transform_single_normal_deep_dream(stft)
            output.append((prediction, sample_rate, mag_max_value, phase))
        return output

    def _transform_single_normal_deep_dream(self, stft: np.ndarray) -> np.ndarray:
        octaves = []
        for i in range(self._n_octaves - 1):
            hw = stft.shape[:2]
            lo = cv2.resize(stft, tuple(np.int32(np.float32(hw[::-1]) / self._octave_scale)))[..., None]
            hi = stft - cv2.resize(lo, tuple(np.int32(hw[::-1])))[..., None]
            stft = lo
            octaves.append(hi)

        for octave in tqdm.trange(self._n_octaves, desc="Image optimisation"):
            if octave > 0:
                hi = octaves[-octave]
                stft = cv2.resize(stft, tuple(np.int32(hi.shape[:2][::-1])))[..., None] + hi

            stft = torch.from_numpy(stft).float()
            if self._use_gpu:
                stft = stft.cuda()
            stft = stft.permute((2, 0, 1))

            for i in tqdm.trange(self._number_of_iterations, desc="Octave optimisation"):
                g = self.calc_grad_tiled(stft)
                g /= (g.abs().mean() + 1e-8)
                g *= self._optimisation_step_size
                stft += g

            if self._use_gpu:
                stft = stft.cpu()

            stft = stft.detach().numpy().transpose((1, 2, 0))

        return stft

    def calc_grad_tiled(self, stft: torch.Tensor, tile_size: int = 128) -> torch.Tensor:
        h, w = stft.shape[1:]
        sx, sy = self._np_rng.randint(tile_size, size=2)
        stft_shift = roll(roll(stft, sx, axis=2), sy, axis=1)
        grads = torch.zeros_like(stft)
        for y in range(0, max(h - tile_size // 2, tile_size), tile_size):
            for x in range(0, max(w - tile_size // 2, tile_size), tile_size):
                frame = stft_shift[:, y:y + tile_size, x:x + tile_size]
                frame.requires_grad = True
                self._classifier(frame[None])

                layer_output = self._available_layers[self._block_name][0, 6]
                objective_output = layer_output.mean()
                objective_output.backward()

                frame.requires_grad = False
                grad = frame.grad.detach().clone()
                grads[:, y:y + tile_size, x:x + tile_size] = grad
        result = roll(roll(grads, -sx, axis=2), -sy, axis=1)
        return result


class Denormalize(TransformerMixin, BaseEstimator):
    def fit(self, x, y=None, **fit_params):
        return self

    @classmethod
    def transform(cls, x: Iterable[Tuple[np.ndarray, int, float, np.ndarray]]) -> Iterable[Tuple[np.ndarray, int]]:
        output = []
        for stft, fs, mag_max_value, phase in x:
            stft = np.flipud(stft)[..., 0]
            stft = (stft + 127.5)
            out = (1 - stft / stft.max()) * mag_max_value
            out = np.power(out, 1 / constants.MAGNITUDE_NONLINEARITY)

            stft = out * phase
            unfouriered = librosa.istft(stft,
                                        win_length=constants.LIBRISPEECH_COMPONENTS,
                                        hop_length=constants.LIBRISPEECH_HOP_LENGTH,
                                        center=True)
            unfouriered = ((unfouriered - unfouriered.min()) / (unfouriered.max() - unfouriered.min()) - 0.5) * 8
            unfouriered = np.clip(unfouriered, -1, 1)
            output.append((unfouriered, fs))
        return output


class SaveResult(TransformerMixin, BaseEstimator):
    def __init__(self, output_dir: str, base_name: str):
        self.output_dir = output_dir
        self.base_name = base_name

    def fit(self, x, y=None, **fit_params):
        return self

    def transform(self, x: Iterable[Tuple[np.ndarray, int]]) -> Iterable[Tuple[np.ndarray, int]]:
        output = []
        for i, (signal, fs) in enumerate(x):
            path = os.path.join(self.output_dir, self.base_name + "_{}.wav".format(i))
            librosa.output.write_wav(path, signal, fs, norm=True)
            output.append((signal, fs))
        return output


def get_processing_pipeline(model_path: str) -> Pipeline:
    return Pipeline([
        ("data load", DataLoader()),
        ("data processor", DataPreprocessor()),
        ("deep dream", Model(
            block_name="residual_1a",
            model_path=model_path,
            n_octaves=10,
            number_of_iterations=10,
            optimisation_step_size=0.6,
            verbose=True,
            use_gpu=False,
        )),
        ("denormalize", Denormalize()),
        ("data saver", SaveResult(".", "audio"))
    ])


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path", help="Path to model")
    parser.add_argument("file_path", help="Path to .wav file to process")
    args = parser.parse_args()

    pipe = get_processing_pipeline(args.model_path)
    print(pipe.transform([args.file_path]))


if __name__ == '__main__':
    main()
