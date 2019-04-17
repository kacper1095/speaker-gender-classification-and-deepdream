from pathlib import Path
from typing import Callable, Optional, List

import librosa
import numpy as np
import pandas as pd
from torchvision.models import inception
from torch.utils.data import Dataset

import constants


class ExtractStft(object):
    def __call__(self, flac: np.ndarray) -> np.ndarray:
        stft = ExtractStft.get_stft(flac)
        return stft

    @staticmethod
    def get_stft(flac: np.ndarray) -> np.ndarray:
        fouriered = librosa.stft(flac, n_fft=constants.LIBRISPEECH_WINDOW_SIZE,
                                 win_length=constants.LIBRISPEECH_WINDOW_SIZE)

        mag, phase = librosa.magphase(fouriered)
        mag = np.power(mag, 1 / 8)
        mag = 1 - mag / mag.max() * 255
        ph = np.angle(phase) / np.pi * 255

        stacked = np.stack((mag, ph), axis=-1)
        return stacked


class RandomCrop(object):
    def __init__(self, crop_width: int, seed: Optional[int] = None):
        self.crop_width = crop_width
        self._rng = np.random.RandomState(seed)

    def __call__(self, spectogram: np.ndarray) -> np.ndarray:
        width = spectogram.shape[1]
        index = self._rng.randint(0, width - self.crop_width)
        return spectogram[:, index:index + self.crop_width]


class LibriSpeechDataset(Dataset):
    def __init__(self, npy_files: List[Path], speakers_info_path: str,
                 transforms: Optional[Callable] = None):
        self._npy_files = npy_files
        self._speakers_data = pd.read_csv(speakers_info_path, delimiter="|",
                                          comment=";", header=None)
        self._speakers_data.columns = ["id", "sex", "dataset", "minutes", "name"]
        self._speakers_data["sex"] = self._speakers_data["sex"].map(str.strip)

        self._id_to_sex_map = {}
        self._sex_to_class = {
            "F": 0,
            "M": 1
        }
        self._speakers_data.apply(lambda row: self._id_to_sex_map.update({
            row["id"]: row["sex"]
        }), axis=1)

        self.transforms = transforms

    def __getitem__(self, index):
        audio_data = np.load(self._npy_files[index])
        speaker_id = int(self._npy_files[index].parent.name)

        if self.transforms is not None:
            audio_data = self.transforms(audio_data)

        sex = self._id_to_sex_map.get(speaker_id, "F")
        a_class = self._sex_to_class[sex]
        audio_data = np.transpose(audio_data, (2, 0, 1))

        return audio_data, a_class

    def __len__(self):
        return len(self._npy_files)