from typing import TYPE_CHECKING, Any, List, Optional, Tuple
import copy

import numpy as np
from numpy.typing import NDArray
import matplotlib.pyplot as plt

from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.pipeline import make_pipeline
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.decomposition import PCA

from dicl.main.iclearner import MultiVariateICLTrainer

if TYPE_CHECKING:
    from transformers import AutoModel, AutoTokenizer


class IdentityTransformer(BaseEstimator, TransformerMixin):
    def __init__(self):
        pass

    def fit(self, input_array: NDArray, y: Optional[NDArray] = None):
        return self

    def transform(self, input_array: NDArray, y: Optional[NDArray] = None) -> NDArray:
        return input_array * 1

    def inverse_transform(
        self, input_array: NDArray, y: Optional[NDArray] = None
    ) -> NDArray:
        return input_array * 1


class DICL:
    def __init__(
        self,
        disentangler: Any,
        n_features: int,
        n_components: int,
        model: "AutoModel",
        tokenizer: "AutoTokenizer",
        rescale_factor: float = 7.0,
        up_shift: float = 1.5,
    ):
        """
        Initialize the TimeSeriesForecaster with a disentangler and an iclearner.

        Parameters:
            disentangler (object): An object that has `transform` and
            `inverse_transform` methods.
        iclearner (object): An object that has a `forecast` method.
        """

        self.n_features = n_features
        self.n_components = n_components
        self.rescale_factor = rescale_factor
        self.up_shift = up_shift

        self.disentangler = make_pipeline(
            MinMaxScaler(), StandardScaler(), disentangler
        )

        self.iclearner = MultiVariateICLTrainer(
            model=model,
            tokenizer=tokenizer,
            n_observations=n_components,
            rescale_factor=rescale_factor,
            up_shift=up_shift,
        )

    def fit_disentangler(self, X: NDArray):
        """
        Fit the disentangler on the input data.

        Parameters:
        X (numpy.ndarray): The input time series data.
        """
        self.disentangler.fit(X)

    def transform(self, X: NDArray) -> NDArray:
        """
        Transform the input data using the disentangler.

        Parameters:
        X (numpy.ndarray): The input time series data.

        Returns:
        numpy.ndarray: The transformed time series data.
        """
        return self.disentangler.transform(X)

    def inverse_transform(self, X_transformed: NDArray) -> NDArray:
        """
        Inverse transform the data back to the original space.

        Parameters:
        X_transformed (numpy.ndarray): The transformed time series data.

        Returns:
        numpy.ndarray: The original time series data.
        """
        return self.disentangler.inverse_transform(X_transformed)

    def predict_single_step(self, X: NDArray) -> Tuple[NDArray, ...]:
        """
        Perform time series forecasting.

        Parameters:
        X (numpy.ndarray): The input time series data.
        horizon (int): The forecasting horizon.

        Returns:
        numpy.ndarray: The forecasted time series data in the original space.
        """
        assert (
            X.shape[1] == self.n_features
        ), f"N features doesnt correspond to {self.n_features}"

        self.context_length = X.shape[0]
        self.X = X

        # Step 1: Transform the time series
        X_transformed = self.transform(X[:-1])

        # Step 2: Perform time series forecasting
        self.iclearner.update_context(
            time_series=copy.copy(X_transformed),
            mean_series=copy.copy(X_transformed),
            sigma_series=np.zeros_like(X_transformed),
            context_length=X_transformed.shape[0],
            update_min_max=True,
        )

        self.iclearner.icl(verbose=0, stochastic=True)

        icl_object = self.iclearner.compute_statistics()

        # Step 3: Inverse transform the predictions
        all_mean = []
        all_mode = []
        all_lb = []
        all_ub = []
        for dim in range(self.n_components):
            ts_max = icl_object[dim].rescaling_max
            ts_min = icl_object[dim].rescaling_min
            # -------------------- Useful for Plots --------------------
            mode_arr = (
                (icl_object[dim].mode_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            mean_arr = (
                (icl_object[dim].mean_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            sigma_arr = (icl_object[dim].sigma_arr.flatten() / self.rescale_factor) * (
                ts_max - ts_min
            )

            all_mean.append(mean_arr[..., None])
            all_mode.append(mode_arr[..., None])
            all_lb.append(mean_arr[..., None] - sigma_arr[..., None])
            all_ub.append(mean_arr[..., None] + sigma_arr[..., None])

        self.mean = self.inverse_transform(np.concatenate(all_mean, axis=1))
        self.mode = self.inverse_transform(np.concatenate(all_mode, axis=1))
        self.lb = self.inverse_transform(np.concatenate(all_lb, axis=1))
        self.ub = self.inverse_transform(np.concatenate(all_ub, axis=1))

        return self.mean, self.mode, self.lb, self.ub

    def plot_single_step(
        self,
        feature_names: Optional[List[str]] = None,
        xlim: Optional[List[float]] = None,
        savefigpath: Optional[str] = None,
    ):
        if not feature_names:
            feature_names = [f"f{i}" for i in range(self.n_features)]

        _, axes = plt.subplots(
            (self.n_features // 3) + 1,
            3,
            figsize=(20, 25),
            gridspec_kw={"hspace": 0.3},
            sharex=True,
        )
        axes = list(np.array(axes).flatten())
        for dim in range(self.n_features):
            ax = axes[dim]
            ax.plot(
                np.arange(self.context_length - 1),
                self.X[1:, dim],
                color="red",
                linewidth=1.5,
                label="groundtruth",
                linestyle="--",
            )
            ax.plot(
                np.arange(self.context_length - 1),
                self.mode[:, dim],
                label=r"mode",
                color="black",
                linestyle="--",
                alpha=0.5,
            )
            ax.plot(
                np.arange(self.context_length - 1),
                self.mean[:, dim],
                label=r"mean $\pm$ std",
            )
            ax.fill_between(
                x=np.arange(self.context_length - 1),
                y1=self.lb[:, dim],
                y2=self.ub[:, dim],
                alpha=0.3,
            )
            ax.set_xlim([0, self.context_length - 1])
            ax.set_ylabel(feature_names[dim], rotation=0, labelpad=20)
            ax.set_yticklabels([])
            if xlim is not None:
                ax.set_xlim(xlim)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=6)
        if savefigpath:
            plt.savefig(savefigpath, bbox_inches="tight")
        plt.show()


class vICL(DICL):
    def __init__(
        self,
        n_features: int,
        n_components: int,
        model: "AutoModel",
        tokenizer: "AutoTokenizer",
        rescale_factor: float = 7.0,
        up_shift: float = 1.5,
    ):
        """
        Initialize DICL with a PCA disentangler and an iclearner.

        Parameters:
            disentangler (object): An object that has `transform` and
            `inverse_transform` methods.
        iclearner (object): An object that has a `predict` method.
        """
        assert n_features == n_components, "vICL doesn't support a different number of"
        f" components ({n_components}) than the number of features ({n_features})"

        super(vICL, self).__init__(
            disentangler=IdentityTransformer(),
            n_features=n_features,
            n_components=n_components,
            model=model,
            tokenizer=tokenizer,
            rescale_factor=rescale_factor,
            up_shift=up_shift,
        )


class DICL_PCA(DICL):
    def __init__(
        self,
        n_features: int,
        n_components: int,
        model: "AutoModel",
        tokenizer: "AutoTokenizer",
        rescale_factor: float = 7.0,
        up_shift: float = 1.5,
    ):
        """
        Initialize DICL with a PCA disentangler and an iclearner.

        Parameters:
            disentangler (object): An object that has `transform` and
            `inverse_transform` methods.
        iclearner (object): An object that has a `predict` method.
        """
        super(DICL_PCA, self).__init__(
            disentangler=PCA(n_components=n_components),
            n_features=n_features,
            n_components=n_components,
            model=model,
            tokenizer=tokenizer,
            rescale_factor=rescale_factor,
            up_shift=up_shift,
        )