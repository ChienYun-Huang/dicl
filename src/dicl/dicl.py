from typing import TYPE_CHECKING, Any, List, Optional, Tuple
import copy

import numpy as np
from numpy.typing import NDArray
import matplotlib.pyplot as plt
import seaborn as sns

from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.pipeline import make_pipeline
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.decomposition import PCA

from dicl.icl.iclearner import MultiVariateICLTrainer
from dicl.utils.calibration import compute_ks_metric, ks_cdf

if TYPE_CHECKING:
    from transformers import AutoModel, AutoTokenizer


class IdentityTransformer(BaseEstimator, TransformerMixin):
    def __init__(self):
        """
        A custom scikit-learn transformer that performs no operation on the data.
        This is useful for the vICL variant of DICL.

        Methods:
            fit(
                input_array: NDArray, y: Optional[NDArray] = None
            ) -> IdentityTransformer:
                Fits the transformer (no-op in this case), returning the instance
                itself.

            transform(input_array: NDArray, y: Optional[NDArray] = None) -> NDArray:
                Returns the input data without modification.

            inverse_transform(
                input_array: NDArray, y: Optional[NDArray] = None) -> NDArray:
                    Returns the input data without modification.
        """
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
    """
    DICL (Disentangled In-Context Learning) is a time-series forecasting model that
    leverages a disentangler and a foundational model (LLM) for time series forecasting.

    Methods:
        fit_disentangler(X: NDArray):
            Fit the disentangler on the input data.

        transform(X: NDArray) -> NDArray:
            Transform the input data using the disentangler.

        inverse_transform(X_transformed: NDArray) -> NDArray:
            Inverse transform the data back to the original space after disentangling.

        predict_single_step(X: NDArray) -> Tuple[NDArray, ...]:
            Perform single-step time series prediction using the model.

        predict_multi_step(
            X: NDArray,
            prediction_horizon: int,
            stochastic: bool = False,
            if_true_mean_else_mode: bool = False
        ) -> Tuple[NDArray, ...]:
            Perform multi-step time series prediction for the given horizon.

        compute_metrics(burnin: int = 0) -> dict:
            Compute evaluation metrics (e.g., MSE, KS test) after prediction.

        plot_single_step(
            feature_names: Optional[List[str]] = None,
            xlim: Optional[List[float]] = None,
            savefigpath: Optional[str] = None
        ):
            Plot single-step predictions and ground truth time series.

        plot_multi_step(
            feature_names: Optional[List[str]] = None,
            xlim: Optional[List[float]] = None,
            savefigpath: Optional[str] = None
        ):
            Plot multi-step predictions and ground truth time series.

        plot_calibration(
            feature_names: Optional[List[str]] = None,
            savefigpath: Optional[str] = None,
            burnin: int = 0
        ):
            Plot the calibration curves based on the KS test for different features.
    """

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
        Initialize the DICL model with the specified disentangler, model, and
        hyperparameters.

        Args:
            disentangler (Any): The disentangler or preprocessor pipeline.
            n_features (int): Number of input features.
            n_components (int): Number of independent components to be learned.
            model (AutoModel): Pretrained model used for time series prediction.
            tokenizer (AutoTokenizer): Tokenizer associated with the pretrained model.
            rescale_factor (float, optional): Rescaling factor for the transformed data.
                Defaults to 7.0.
            up_shift (float, optional): Shift factor applied to rescaled data.
                Defaults to 1.5.
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
            n_features=n_components,
            rescale_factor=rescale_factor,
            up_shift=up_shift,
        )

    def fit_disentangler(self, X: NDArray):
        """
        Fit the disentangler on the input data.

        Args:
            X (NDArray): Input time series data.
        """
        self.disentangler.fit(X)

    def transform(self, X: NDArray) -> NDArray:
        """
        Transform the input data using the disentangler.

        Args:
            X (NDArray): Input time series data to be transformed.

        Returns:
            NDArray: Transformed data.
        """
        return self.disentangler.transform(X)

    def inverse_transform(self, X_transformed: NDArray) -> NDArray:
        """
        Inverse transform the data back to its original representation.

        Args:
            X_transformed (NDArray): Transformed time series data.

        Returns:
            NDArray: Data transformed back to the original space.
        """
        return self.disentangler.inverse_transform(X_transformed)

    def predict_single_step(self, X: NDArray) -> Tuple[NDArray, ...]:
        """
        Perform single-step prediction on the input data.

        Args:
            X (NDArray): Input time series data.

        Returns:
            Tuple[NDArray, ...]: Mean, mode, lower bound, and upper bound of the
                predictions.
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

        self.icl_object = self.iclearner.compute_statistics()

        # Step 3: Inverse transform the predictions
        all_mean = []
        all_mode = []
        all_lb = []
        all_ub = []
        for dim in range(self.n_components):
            ts_max = self.icl_object[dim].rescaling_max
            ts_min = self.icl_object[dim].rescaling_min
            # -------------------- Useful for Plots --------------------
            mode_arr = (
                (self.icl_object[dim].mode_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            mean_arr = (
                (self.icl_object[dim].mean_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            sigma_arr = (
                self.icl_object[dim].sigma_arr.flatten() / self.rescale_factor
            ) * (ts_max - ts_min)

            all_mean.append(mean_arr[..., None])
            all_mode.append(mode_arr[..., None])
            all_lb.append(mean_arr[..., None] - sigma_arr[..., None])
            all_ub.append(mean_arr[..., None] + sigma_arr[..., None])

        self.mean = self.inverse_transform(np.concatenate(all_mean, axis=1))
        self.mode = self.inverse_transform(np.concatenate(all_mode, axis=1))
        self.lb = self.inverse_transform(np.concatenate(all_lb, axis=1))
        self.ub = self.inverse_transform(np.concatenate(all_ub, axis=1))

        return self.mean, self.mode, self.lb, self.ub

    def predict_multi_step(
        self,
        X: NDArray,
        prediction_horizon: int,
        stochastic: bool = False,
        if_true_mean_else_mode: bool = False,
    ) -> Tuple[NDArray, ...]:
        """
        Perform multi-step prediction for a given horizon.

        Args:
            X (NDArray): Input time series data.
            prediction_horizon (int): Number of steps to predict into the future
                (taken from the end of the input time series).
            stochastic (bool, optional): Whether to apply stochastic predictions.
                Defaults to False.
            if_true_mean_else_mode (bool, optional): Whether to return mean predictions
                (True) or mode predictions (False). Defaults to False.

        Returns:
            Tuple[NDArray, ...]: Mean, mode, lower bound, and upper bound of the
                predictions.
        """
        assert (
            X.shape[1] == self.n_features
        ), f"N features doesnt correspond to {self.n_features}"

        self.context_length = X.shape[0]
        self.X = X
        self.prediction_horizon = prediction_horizon

        # Step 1: Transform the time series
        X_transformed = self.transform(X[: -1 - prediction_horizon])

        # Step 2: Perform time series forecasting
        self.iclearner.update_context(
            time_series=copy.copy(X_transformed),
            mean_series=copy.copy(X_transformed),
            sigma_series=np.zeros_like(X_transformed),
            context_length=X_transformed.shape[0],
            update_min_max=True,
        )

        self.iclearner.icl(
            stochastic=stochastic,
            if_true_mean_else_mode=if_true_mean_else_mode,
            verbose=0,
        )

        self.icl_object = self.iclearner.predict_long_horizon_llm(
            prediction_horizon=prediction_horizon,
            stochastic=stochastic,
            if_true_mean_else_mode=if_true_mean_else_mode,
            verbose=1,
        )

        # Step 3: Inverse transform the predictions
        all_mean = []
        all_mode = []
        all_lb = []
        all_ub = []
        for dim in range(self.n_components):
            ts_max = self.icl_object[dim].rescaling_max
            ts_min = self.icl_object[dim].rescaling_min
            # -------------------- Useful for Plots --------------------
            mode_arr = (
                (self.icl_object[dim].mode_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            mean_arr = (
                (self.icl_object[dim].mean_arr.flatten() - self.up_shift)
                / self.rescale_factor
            ) * (ts_max - ts_min) + ts_min
            sigma_arr = (
                self.icl_object[dim].sigma_arr.flatten() / self.rescale_factor
            ) * (ts_max - ts_min)

            all_mean.append(mean_arr[..., None])
            all_mode.append(mode_arr[..., None])
            all_lb.append(mean_arr[..., None] - sigma_arr[..., None])
            all_ub.append(mean_arr[..., None] + sigma_arr[..., None])

        self.mean = self.inverse_transform(np.concatenate(all_mean, axis=1))
        self.mode = self.inverse_transform(np.concatenate(all_mode, axis=1))
        self.lb = self.inverse_transform(np.concatenate(all_lb, axis=1))
        self.ub = self.inverse_transform(np.concatenate(all_ub, axis=1))

        return self.mean, self.mode, self.lb, self.ub

    def compute_metrics(self, burnin: int = 0):
        """
        Compute the prediction metrics such as MSE and KS test.

        Args:
            burnin (int, optional): Number of initial steps to ignore when computing
                metrics. Defaults to 0.

        Returns:
            dict: Dictionary containing various prediction metrics.
        """
        metrics = {}

        # ------- MSE --------

        perdim_squared_errors = (self.X[1:] - self.mean) ** 2
        agg_squared_error = np.linalg.norm(self.X[1:] - self.mean, axis=1)

        metrics["average_agg_squared_error"] = agg_squared_error[burnin:].mean(axis=0)
        metrics["agg_squared_error"] = agg_squared_error
        metrics["average_perdim_squared_error"] = perdim_squared_errors[burnin:].mean(
            axis=0
        )
        metrics["perdim_squared_error"] = agg_squared_error

        # ------ KS -------
        kss, _ = compute_ks_metric(
            groundtruth=self.X[1:],
            icl_object=self.icl_object,
            n_components=self.n_components,
            n_features=self.n_features,
            inverse_transform=self.inverse_transform,
            burnin=burnin,
        )

        metrics["perdim_ks"] = kss
        metrics["agg_ks"] = kss.mean(axis=0)

        return metrics

    def plot_single_step(
        self,
        feature_names: Optional[List[str]] = None,
        xlim: Optional[List[float]] = None,
        savefigpath: Optional[str] = None,
    ):
        """
        Plot single-step predictions against ground truth.

        Args:
            feature_names (Optional[List[str]], optional): Names of the features.
                Defaults to None.
            xlim (Optional[List[float]], optional): X-axis limits.
                Defaults to None.
            savefigpath (Optional[str], optional): File path to save the plot.
                Defaults to None.
        """
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
                color=sns.color_palette("colorblind")[0],
            )
            ax.fill_between(
                x=np.arange(self.context_length - 1),
                y1=self.lb[:, dim],
                y2=self.ub[:, dim],
                alpha=0.3,
                color=sns.color_palette("colorblind")[0],
            )
            ax.set_ylabel(feature_names[dim], rotation=0, labelpad=20)
            ax.set_yticklabels([])
            if xlim is not None:
                ax.set_xlim(xlim)
            else:
                ax.set_xlim([0, self.context_length - 1])
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=6)
        if savefigpath:
            plt.savefig(savefigpath, bbox_inches="tight")
        plt.show()

    def plot_multi_step(
        self,
        feature_names: Optional[List[str]] = None,
        xlim: Optional[List[float]] = None,
        savefigpath: Optional[str] = None,
    ):
        """
        Plot multi-step predictions and ground truth.

        Args:
            feature_names (Optional[List[str]], optional): Names of the features.
                Defaults to None.
            xlim (Optional[List[float]], optional): X-axis limits.
                Defaults to None.
            savefigpath (Optional[str], optional): File path to save the plot.
                Defaults to None.
        """
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
                np.arange(self.context_length - self.prediction_horizon),
                self.mean[: self.context_length - self.prediction_horizon, dim],
                label=r"mean $\pm$ std",
                color=sns.color_palette("colorblind")[0],
            )
            ax.fill_between(
                x=np.arange(self.context_length - self.prediction_horizon),
                y1=self.lb[: self.context_length - self.prediction_horizon, dim],
                y2=self.ub[: self.context_length - self.prediction_horizon, dim],
                alpha=0.3,
                color=sns.color_palette("colorblind")[0],
            )
            ax.plot(
                np.arange(
                    self.context_length - self.prediction_horizon - 1,
                    self.context_length - 1,
                ),
                self.mean[-self.prediction_horizon :, dim],
                label="multi-step",
                color=sns.color_palette("colorblind")[1],
            )
            ax.fill_between(
                np.arange(
                    self.context_length - self.prediction_horizon - 1,
                    self.context_length - 1,
                ),
                y1=self.lb[-self.prediction_horizon :, dim],
                y2=self.ub[-self.prediction_horizon :, dim],
                alpha=0.3,
                color=sns.color_palette("colorblind")[1],
            )
            ax.set_ylabel(feature_names[dim], rotation=0, labelpad=20)
            ax.set_yticklabels([])
            if xlim is not None:
                ax.set_xlim(xlim)
            else:
                ax.set_xlim([0, self.context_length - 1])
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=6)
        if savefigpath:
            plt.savefig(savefigpath, bbox_inches="tight")
        plt.show()

    def plot_calibration(
        self,
        feature_names: Optional[List[str]] = None,
        savefigpath: Optional[str] = None,
        burnin: int = 0,
    ):
        """
        Plot calibration curves based on the KS test.

        Args:
            feature_names (Optional[List[str]], optional): Names of the features.
                Defaults to None.
            savefigpath (Optional[str], optional): File path to save the plot.
                Defaults to None.
            burnin (int, optional): Number of initial steps to ignore when computing
                calibration. Defaults to 0.
        """
        kss, ks_quantiles = compute_ks_metric(
            groundtruth=self.X[1:],
            icl_object=self.icl_object,
            n_components=self.n_components,
            n_features=self.n_features,
            inverse_transform=self.inverse_transform,
            burnin=burnin,
        )

        if not feature_names:
            feature_names = [f"f{i}" for i in range(self.n_features)]

        _, axes = plt.subplots(
            (self.n_features // 3) + 1,
            3,
            figsize=(20, 20),
            gridspec_kw={"wspace": 0.2, "hspace": 1.0},
            sharex=True,
            sharey=True,
        )
        axes = list(np.array(axes).flatten())
        for dim in range(self.n_features):
            ks_cdf(
                ks_quantiles,
                dim,
                ax=axes[dim],
                verbose=0,
                pot_cdf_uniform=True,
                color=sns.color_palette("colorblind")[0],
                label=f"llm $ks={kss[dim]:.2f}$",
            )
            axes[dim].set_title(f"{feature_names[dim]}")
            axes[dim].grid(True)
            if dim >= (self.n_features - 3):
                axes[dim].set_xlabel("quantile")
                axes[dim].legend(
                    loc="upper center", bbox_to_anchor=(0.5, -0.68), ncol=6, fontsize=9
                )
            else:
                axes[dim].legend(
                    loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=6, fontsize=9
                )
            if dim % 3 == 0:
                axes[dim].set_ylabel("proportion")
        if savefigpath:
            plt.savefig(savefigpath, bbox_inches="tight")
        plt.show()


class vICL(DICL):
    def __init__(
        self,
        n_features: int,
        model: "AutoModel",
        tokenizer: "AutoTokenizer",
        rescale_factor: float = 7.0,
        up_shift: float = 1.5,
    ):
        """
        Initialize DICL with no disentangler (IdentityTransformer).

        Args:
            n_features (int): Number of input features.
            model (AutoModel): Pretrained model used for time series prediction.
            tokenizer (AutoTokenizer): Tokenizer associated with the pretrained model.
            rescale_factor (float, optional): Rescaling factor for the transformed data.
                Defaults to 7.0.
            up_shift (float, optional): Shift factor applied to rescaled data.
                Defaults to 1.5.
        """
        super(vICL, self).__init__(
            disentangler=IdentityTransformer(),
            n_features=n_features,
            n_components=n_features,
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
        Initialize DICL with a PCA disentangler.

        Args:
            n_features (int): Number of input features.
            n_components (int): Number of independent components to be learned.
            model (AutoModel): Pretrained model used for time series prediction.
            tokenizer (AutoTokenizer): Tokenizer associated with the pretrained model.
            rescale_factor (float, optional): Rescaling factor for the transformed data.
                Defaults to 7.0.
            up_shift (float, optional): Shift factor applied to rescaled data.
                Defaults to 1.5.
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
